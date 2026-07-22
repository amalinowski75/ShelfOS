// Shared Add/Take stock dialog (spec §14-15). Exposes
// `openStockDialog(mode, componentId, onSaved)`; `onSaved` runs after a successful
// write so each page can refresh however it needs (the components list reloads its
// Tabulator feed, a detail page reloads itself).
//
// Also wires any `[data-stock-act]` button on the page — that's the declarative
// entry point for server-rendered pages, whose tables have no JSON feed to re-pull,
// so those reload. `csrfToken` and `errorMessage` come from shared.js.
(function () {
  const dialog = document.getElementById("stock-dialog");
  if (!dialog) return; // read-only accounts never get the dialog rendered
  const form = document.getElementById("stock-form");
  const errorEl = document.getElementById("stock-error");
  const title = document.getElementById("stock-dialog-title");

  let onSaved = null;
  // Ignore a re-entrant submit while this form's write is in flight, enough to stop
  // a fast double-click sending a duplicate POST. The flag is the dialog's OWN: an
  // in-flight stock write must never swallow another dialog's submit.
  let inFlight = false;
  // A reload is a *pending* navigation, not a finished one: the page stays live and
  // clickable until the server answers. Without this the in-flight flag would have
  // been released already and a second click would post a duplicate movement.
  let navigating = false;
  // Monotonic: a location-usage lookup that lands after the dialog was reopened for
  // a different component (or mode) must not apply its filter.
  let openToken = 0;
  // How long to wait for the location-usage lookup before giving up and showing
  // the whole tree. Long enough not to fire on a merely slow answer, short enough
  // that a hang doesn't read as "the dialog is broken".
  const USAGE_TIMEOUT_MS = 5000;

  // …but a requested reload isn't guaranteed to land either: Stop/Esc, a dropped
  // connection, or a back/forward-cache restore all leave this page rendered and
  // interactive. Left latched, every stock action would then be silently dead. The
  // page is alive again here, so let go. (pageshow also fires on a bfcache restore,
  // which no other load event does.)
  window.addEventListener("pageshow", () => {
    navigating = false;
  });

  window.openStockDialog = function (mode, componentId, callback) {
    if (navigating) return;
    onSaved = callback || null;
    form.component_id.value = componentId;
    form.mode.value = mode;
    title.textContent = mode === "add" ? "Add stock" : "Take from stock";
    errorEl.hidden = true;
    form.quantity.value = 1;
    form.note.value = ""; // else a note leaks into the NEXT movement
    form.querySelector(".loc-picker")?.reset(); // also drops the previous filter
    dialog.showModal();
    // Bumped HERE, not inside the helper: an early return below must still
    // invalidate a lookup that is already in flight from a previous open.
    narrowLocations(mode, componentId, ++openToken);
  };

  // Offer only the locations that make sense for this mode: Take from where the
  // part actually is, Add into a free slot (or back into the one it already
  // occupies). Which locations those are depends on the component, so it can't be
  // baked into the server-rendered picker and is fetched per open.
  //
  // Advisory: the picker offers "show all locations", and the tree stays unfiltered
  // if this fetch fails — a lookup for a nicety must never block the write itself.
  async function narrowLocations(mode, componentId, token) {
    const picker = form.querySelector(".loc-picker");
    if (!picker?.setFilter) return;
    picker.setBusy?.(true);
    let usage;
    // fetch has no timeout of its own, and a HANG is worse here than a failure:
    // the picker would stay disabled forever, so no location could be chosen and
    // no stock could be moved until the user reloaded — the write held hostage by
    // a convenience. A tarpitting proxy or a captive portal does exactly that.
    // The race (not just the abort) is what guarantees this resolves, whatever the
    // request ends up doing.
    const controller = new AbortController();
    let expire;
    const expired = new Promise((resolve) => {
      expire = setTimeout(() => {
        controller.abort();
        resolve(undefined);
      }, USAGE_TIMEOUT_MS);
    });
    try {
      const resp = await Promise.race([
        fetch(`/web/api/components/${componentId}/location-usage`, {
          signal: controller.signal,
        }),
        expired,
      ]);
      if (resp?.ok) usage = await resp.json();
    } catch {
      usage = undefined;
    } finally {
      clearTimeout(expire);
    }
    // A slow answer must not filter (or un-busy) a dialog reopened since.
    if (token !== openToken) return;
    picker.setBusy?.(false);
    // Anything but the expected shape leaves the tree unfiltered rather than
    // throwing past this point and stranding the picker half-configured.
    if (!Array.isArray(usage?.holding) || !Array.isArray(usage?.occupied)) return;
    const holding = new Set(usage.holding);
    const occupied = new Set(usage.occupied);
    picker.setFilter(
      mode === "take"
        ? (id) => holding.has(id)
        : // Free, or already holding this same part so a restock can go back in.
          (id) => !occupied.has(id) || holding.has(id),
    );
  }

  // [data-close] buttons are wired once in shared.js.

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (inFlight || navigating) return;
    // The tree-picker is backed by a hidden input, so enforce the required
    // location here (there's no native `required` to lean on).
    if (form.location_id.value === "") {
      errorEl.textContent = "Choose a location.";
      errorEl.hidden = false;
      return;
    }
    inFlight = true;
    // Deliberately narrow: ONLY the write is guarded here. Wrapping the refresh too
    // would catch an onSaved rejection after the dialog has closed, writing "Could
    // not reach the server" into an element nobody can see — and mislabelling it,
    // since the server plainly was reached: the movement is already saved.
    let resp;
    try {
      const url = form.mode.value === "add" ? "/api/stock/add" : "/api/stock/remove";
      resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          component_id: Number(form.component_id.value),
          location_id: Number(form.location_id.value),
          quantity: Number(form.quantity.value),
          note: form.note.value || null,
        }),
      });
    } catch {
      // A rejected fetch (offline, DNS, aborted) would otherwise leave the dialog
      // sitting there unchanged, which reads as "nothing happened" and invites a
      // second click on a write that may or may not have landed.
      errorEl.textContent = "Could not reach the server.";
      errorEl.hidden = false;
      return;
    } finally {
      inFlight = false;
    }

    if (!resp.ok) {
      // errorMessage() outside the catch too: a non-JSON body throwing in there
      // would relabel an HTTP error as a network failure.
      errorEl.textContent = await errorMessage(resp);
      errorEl.hidden = false;
      return;
    }
    dialog.close();
    try {
      await onSaved?.();
    } catch {
      // The write landed; only the refresh didn't. A toast, not the dialog's error
      // line — the dialog is closed by now, so an inline message would never be
      // seen and the user would be left with a stale table and no explanation.
      showToast("Stock saved, but the page couldn't refresh. Reload to see it.");
    }
  });

  // Declarative triggers: `data-stock-act="add|take"` + `data-component-id`. This is
  // the entry point for server-rendered pages, whose stock tables have no JSON feed.
  document.querySelectorAll("[data-stock-act]").forEach((button) => {
    const componentId = Number(button.dataset.componentId);
    // A button with no (or a junk) id would post component_id: null and earn an
    // opaque 422; leaving it inert makes the markup bug obvious instead.
    if (!Number.isFinite(componentId)) return;
    button.addEventListener("click", () =>
      window.openStockDialog(button.dataset.stockAct, componentId, () => {
        // Only a reload can show the write: both stock tables were rendered by
        // Jinja. Block further submits until the new page actually arrives.
        navigating = true;
        window.location.reload();
      }),
    );
  });
})();

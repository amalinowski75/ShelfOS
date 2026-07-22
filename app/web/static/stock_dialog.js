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
    form.querySelector(".loc-picker")?.reset();
    dialog.showModal();
  };

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

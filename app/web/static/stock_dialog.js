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
  const locScan = document.getElementById("stock-loc-scan");
  const locScanMsg = document.getElementById("stock-loc-scan-msg");

  let onSaved = null;
  // The per-mode predicate narrowLocations applied to the picker (Take: only where
  // the part is stocked; Add: only free/own slots). Kept so a SCANNED location is
  // held to the same rule the manual picker enforces. null = unfiltered (or the
  // usage lookup hasn't answered yet), in which case the server is the check.
  let locationFilter = null;
  // Resolves when the per-mode filter above has settled for the current open, so a
  // shelf scanned before the usage lookup lands can wait for it instead of racing
  // it (see the scan handler). Reassigned on every open.
  let usageReady = Promise.resolve();
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
    locationFilter = null; // narrowLocations sets it once the usage lookup lands
    if (locScan) locScan.value = "";
    if (locScanMsg) locScanMsg.hidden = true;
    dialog.showModal();
    // Focus the scan field so a wedge scanner's SL… payload lands there, not in the
    // quantity box (a manual user can pick from the tree or type the count instead).
    if (locScan) locScan.focus();
    // Bumped HERE, not inside the helper: an early return below must still
    // invalidate a lookup that is already in flight from a previous open.
    usageReady = narrowLocations(mode, componentId, ++openToken);
  };

  // The scan field's focus is load-bearing: while it holds focus, scan_putaway's
  // page-level collector stands down (a foreign INPUT owns the keyboard) and the
  // wedge scanner's SL… payload lands HERE. A click on dead space inside the dialog
  // would drop focus to <body>; pull it back so a scan can never drift to the
  // putaway panel behind this modal.
  dialog.addEventListener("click", () => {
    if (!locScan || !dialog.open) return;
    const active = document.activeElement;
    if (active === document.body || active === dialog) locScan.focus();
  });

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
    const predicate =
      mode === "take"
        ? (id) => holding.has(id)
        : // Free, or already holding this same part so a restock can go back in.
          (id) => !occupied.has(id) || holding.has(id);
    locationFilter = predicate; // hold a scan to the same rule as the manual picker
    picker.setFilter(predicate);
  }

  // A scanned SL… shelf label selects that location in the picker (the manual pick
  // still works — both set the same hidden location_id).
  function setScanMsg(text, isError) {
    if (!locScanMsg) return;
    locScanMsg.textContent = text;
    locScanMsg.className = isError ? "error" : "muted";
    locScanMsg.hidden = false;
  }

  locScan?.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter") return;
    // A wedge scanner ends its payload with Enter — select the location, never
    // submit the half-filled form.
    event.preventDefault();
    const raw = locScan.value;
    locScan.value = "";
    if (!raw.trim()) return; // a stray terminator, or an empty field
    const id = shelfLabelId(raw); // shelfLabelId owns the trim + SL… parse
    if (id === null) {
      setScanMsg(`"${raw.trim()}" isn't a location label (SL…).`, true);
      return;
    }
    // The per-mode filter is fetched async on open, and a scanner can fire before
    // it lands. Wait for it, so the shelf is judged against the mode's rule and not
    // the still-unfiltered tree — otherwise a selection made now would be silently
    // dropped the instant the filter arrived, stranding a "Location set" message
    // beside an empty picker. On the fast path (filter already settled) this
    // resolves on the next microtask, before any paint, so "Checking…" never shows.
    const token = openToken;
    setScanMsg("Checking the location…", false);
    await usageReady.catch(() => {});
    // A reopen for a different component/mode bumps the token: a scan that was
    // waiting must not apply to the dialog that replaced the one it was aimed at.
    // (A plain close without reopen is harmless — the next open resets the picker.)
    if (token !== openToken) return;
    const picker = form.querySelector(".loc-picker");
    const path = picker?.pathOf(id);
    if (path == null) {
      setScanMsg(`Unknown location SL${id} — reprint the label?`, true);
      return;
    }
    if (locationFilter && !locationFilter(id)) {
      if (form.mode.value === "take") {
        // A Take from a shelf the part isn't stocked in can't succeed, so leave
        // the field empty rather than fill a location that would only be cleared.
        setScanMsg(`This part isn't stocked in ${path}.`, true);
        return;
      }
      // Add mode: "one part per slot" is an advisory default the write endpoint
      // accepts, and the scan names the real shelf the user is standing at. Fill
      // it so they aren't forced to re-pick by hand, and keep the warning to
      // explain the flag.
      picker.setValue(id);
      setScanMsg(`${path} already holds another part.`, true);
      return;
    }
    picker.setValue(id);
    setScanMsg(`Location set: ${path}`, false);
  });

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

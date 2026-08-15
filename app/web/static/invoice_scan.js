// Scan-driven putaway on a draft invoice: scan a bag's barcode → the matching
// line's "Set location" dialog opens; scan an SL<id> location label → the
// location is saved, the dialog closes, ready for the next bag. Uses shared.js
// helpers (csrfToken, errorMessage, showToast).
//
// The wedge scanner is a keyboard, but this deliberately does NOT type into a
// focused input. Keystrokes are collected in a document-level capture-phase
// buffer and the (readonly) fields only DISPLAY it. Field tested: password
// managers (Keeper) pounce on a programmatically focused input and shove their
// own overlay iframe in front of it — focus leaves the document entirely
// (document.hasFocus() === false with the ring still painted) and every
// keystroke vanishes into the extension. No focused input, no overlay, no
// stolen scans — and where focus happens to rest stops mattering at all.
(() => {
  const panel = document.getElementById("invoice-scan");
  if (!panel) return; // read-only viewer, finalized invoice, or no lines

  const invoiceId = document.getElementById("invoice-detail").dataset.invoiceId;
  const scanInput = document.getElementById("invoice-scan-input");
  const statusEl = document.getElementById("invoice-scan-status");
  const dialog = document.getElementById("putaway-dialog");
  const form = document.getElementById("putaway-form");
  const partEl = document.getElementById("putaway-part");
  const descEl = document.getElementById("putaway-desc");
  const locationInput = document.getElementById("putaway-scan");
  const locationSelect = document.getElementById("putaway-select");
  const errorEl = document.getElementById("putaway-error");

  // id → human path, for showing where the part just went. The server
  // re-validates the id on save, so this map is presentation, not authority.
  const LOCATIONS = new Map(
    JSON.parse(panel.dataset.locations || "[]").map((o) => [String(o.id), o.path]),
  );
  // What our own location labels encode (see app/services/label_service.py).
  const SL = /^SL(\d+)$/i;

  let target = null; // { kind: "import" | "line", row }
  let busy = false;
  let buffer = ""; // keystrokes collected since the last Enter
  let queued = null; // a bag scan that arrived while the previous one was busy
  let lastKeyAt = -Infinity;
  let idleTimer = null;

  // Give up on a hung request rather than holding `busy` forever — a stuck
  // flag would silently eat every scan that follows.
  const FETCH_TIMEOUT_MS = 10_000;
  // A wedge scanner types faster than a human, but NOT uniformly: real
  // payloads show occasional long gaps (HID polling, a layout switch for ":"
  // or "/"), so this threshold decides one thing only — whether a keypress is
  // deliberate enough to be left to a focused control (Space activating a
  // button, a select's type-ahead). It NEVER splits a payload: characters are
  // appended unconditionally, because a buffer reset mid-scan truncates the
  // code and was exactly that bug.
  const BURST_GAP_MS = 250;
  // An Enter this long after the last character terminates nothing — the
  // buffer is human leftovers, not a scan.
  const TERMINATOR_MS = 400;
  // A buffer nobody terminated is dropped, so stray keystrokes can never
  // prefix the next scan. Far longer than any gap inside a real payload.
  const IDLE_RESET_MS = 1200;

  const norm = (value) => (value || "").trim().toUpperCase();

  // The live field displays the buffer; the idle one sits empty. Both are
  // readonly — they are gauges, not text entry.
  function render() {
    const live = dialog.open ? locationInput : scanInput;
    const idle = dialog.open ? scanInput : locationInput;
    live.value = buffer;
    idle.value = "";
    live.classList.add("scan-armed");
    idle.classList.remove("scan-armed");
  }

  // Both message slots keep their real content in a variable and repaint from
  // it. The window-focus warning below borrows the slots transiently, and an
  // alt-tab round trip must never erase a genuine "unknown location" or save
  // error the user has not acted on yet.
  let statusText = "";
  let statusTone = "";
  let dialogError = "";

  function paintStatus(message, tone) {
    statusEl.textContent = message;
    statusEl.className = tone === "error" ? "error" : "muted";
    statusEl.hidden = !message;
  }

  function setStatus(message, tone) {
    statusText = message;
    statusTone = tone;
    paintStatus(message, tone);
  }

  function paintDialogError(message) {
    errorEl.textContent = message;
    errorEl.hidden = !message;
  }

  function setDialogError(message) {
    dialogError = message;
    paintDialogError(message);
  }

  // Drop a buffer that never got its terminator (a stray keypress, an aborted
  // scan), so it cannot prefix the next code. Rearmed on every keystroke, so
  // it can only fire between scans, never inside one.
  function armIdleReset() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (!buffer) return;
      buffer = "";
      render();
    }, IDLE_RESET_MS);
  }

  // A control whose own keyboard behaviour a deliberate keypress should keep.
  // Inside the putaway dialog nothing qualifies: there the keyboard exists to
  // scan (its buttons stay mouse- and Escape-operated), and a payload's stray
  // Space must never "click" the close button that showModal focused.
  function isPageControl(node) {
    if (!node || dialog.contains(node)) return false;
    return ["BUTTON", "SELECT", "A", "SUMMARY"].includes(node.tagName);
  }

  function rowLabel(row) {
    return (
      row.dataset.mpn ||
      row.dataset.spn ||
      row.querySelector("a, .mono")?.textContent.trim() ||
      "line"
    );
  }

  function hasLocation(match) {
    if (match.kind === "import")
      return !!match.row.querySelector(".ril-location")?.value;
    return !!match.row.dataset.locationId;
  }

  // Match the parsed identifiers against the review rows first (imported bags
  // under review are this feature's main audience), then the regular lines.
  // Among several hits — the same part can appear on two lines — prefer one
  // that still lacks a location, so repeated scans walk through duplicates.
  function findRow(keys) {
    const matches = [];
    for (const row of document.querySelectorAll(
      "#invoice-review tr[data-import-line-id]",
    )) {
      if (keys.has(norm(row.dataset.mpn)) || keys.has(norm(row.dataset.spn)))
        matches.push({ kind: "import", row });
    }
    for (const row of document.querySelectorAll("#invoice-lines tr[data-line-id]")) {
      if (keys.has(norm(row.dataset.mpn)) || keys.has(norm(row.dataset.spn)))
        matches.push({ kind: "line", row });
    }
    return matches.find((m) => !hasLocation(m)) || matches[0] || null;
  }

  function openPutaway(match) {
    target = match;
    partEl.textContent = rowLabel(match.row);
    descEl.textContent = match.row.dataset.description || "";
    descEl.hidden = !descEl.textContent;
    locationSelect.value =
      match.kind === "import"
        ? match.row.querySelector(".ril-location")?.value || ""
        : match.row.dataset.locationId || "";
    setDialogError("");
    buffer = "";
    dialog.showModal();
    render(); // no focus() — see the header comment
  }

  // An error the user must not miss mid-scanning: the small status line plus
  // a toast (eyes are on the bags, not on the panel).
  function scanError(message) {
    setStatus(message, "error");
    showToast(message, { tone: "warn" });
  }

  // Run a queued bag scan once nothing else is in flight and no dialog is up.
  function drainQueue() {
    if (queued && !busy && !dialog.open) {
      const code = queued;
      queued = null;
      handleBagScan(code);
    }
  }

  async function handleBagScan(code) {
    if (!code) return;
    if (busy || dialog.open) {
      // NEVER drop a scan silently — hold the latest one and run it as soon
      // as the current work (a parse, or a dialog mid-close) is done.
      queued = code;
      setStatus("One moment — finishing the previous scan…");
      return;
    }
    if (SL.test(code)) {
      scanError("That is a location label — scan a bag first.");
      return;
    }
    busy = true;
    setStatus("Reading…");
    try {
      const resp = await fetch("/api/shops/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ code }),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!resp.ok) {
        scanError(await errorMessage(resp));
        return;
      }
      const parsed = await resp.json();
      // A QR that is only the product URL still has an identifier: the shop's
      // own symbol, somewhere in the path. Strictly a FALLBACK — the path also
      // yields category/manufacturer segments, and letting those compete with
      // a real part number could file the bag against the wrong line.
      const identifiers = [parsed.mpn, parsed.distributor_pn].filter(Boolean);
      const keys = new Set(
        (identifiers.length ? identifiers : parsed.url_symbols || []).map(norm),
      );
      if (!keys.size) {
        scanError("No part number in this code.");
        return;
      }
      const match = findRow(keys);
      if (!match) {
        scanError(`No line on this invoice matches ${[...keys].join(" / ")}.`);
        return;
      }
      setStatus("");
      openPutaway(match);
    } catch {
      scanError("Could not reach the server — scan again.");
    } finally {
      busy = false;
      drainQueue();
    }
  }

  function handleLocationScan(code) {
    if (busy) {
      // Same contract as bag scans: a scan is never dropped in silence. It is
      // not queued, because by the time the in-flight save lands this dialog
      // may be gone and the code would apply to the wrong part.
      setDialogError("Still saving — scan the location label again in a moment.");
      return;
    }
    const matched = SL.exec(code);
    if (!matched) {
      setDialogError("That is not a location label — scan the SL… code on the shelf.");
      return;
    }
    saveLocation(Number(matched[1]));
  }

  function applyToRow(match, locationId, path) {
    if (match.kind === "import") {
      const select = match.row.querySelector(".ril-location");
      if (select) select.value = String(locationId);
      // Same completeness rule invoices.js applies after its own edits.
      match.row.classList.toggle("is-incomplete", !match.row.dataset.typeId);
    } else {
      match.row.dataset.locationId = String(locationId);
      const cell = match.row.children[2]; // the Location column
      if (cell) cell.textContent = path;
    }
  }

  async function saveLocation(locationId) {
    if (busy || !target) return;
    // Pin the row for the whole round trip: the dialog's own close listener
    // nulls `target`, so a Cancel/Escape landing while the save is in flight
    // would otherwise turn a successful save into a phantom network error and
    // leave the row showing no location the server has already recorded.
    const saving = target;
    const path = LOCATIONS.get(String(locationId));
    if (!path) {
      setDialogError(`Unknown location code SL${locationId} — reprint the label?`);
      return;
    }
    busy = true;
    try {
      const url =
        saving.kind === "import"
          ? `/api/invoices/${invoiceId}/import-lines/${saving.row.dataset.importLineId}`
          : `/api/invoices/${invoiceId}/lines/${saving.row.dataset.lineId}/location`;
      const resp = await fetch(url, {
        method: saving.kind === "import" ? "PATCH" : "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ location_id: locationId }),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!resp.ok) {
        setDialogError(await errorMessage(resp));
        return;
      }
      applyToRow(saving, locationId, path);
      const label = rowLabel(saving.row);
      if (target === saving) target = null;
      dialog.close(); // a no-op if the user already closed it
      showToast(`${label} → ${path}`, { tone: "ok" });
    } catch {
      setDialogError("Could not reach the server.");
    } finally {
      busy = false;
      drainQueue();
    }
  }

  // The collector. Capture phase on the document, so it sees every keystroke
  // before any control can swallow it, wherever focus happens to rest — the
  // whole point of the design (see the header comment). It stands down where
  // the user is genuinely typing: any OTHER open dialog owns its keys, and so
  // do free-text areas and inputs that aren't ours.
  //
  // Within that, every key is APPENDED — a scanner's payload is never split,
  // whatever its internal timing — and only an unterminated buffer expires
  // (armIdleReset). Speed decides one narrower question: an isolated keypress
  // on a page control outside this flow's dialog is the user working the UI,
  // so it is left to that control (Space activates a button, a select's
  // type-ahead runs); everything else is consumed, so a payload's characters
  // can never click a button or steer a select.
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.ctrlKey || event.altKey || event.metaKey) return;
      const t = event.target instanceof HTMLElement ? event.target : null;
      if (t) {
        const openDialog = t.closest("dialog[open]");
        if (openDialog && openDialog !== dialog) return;
        if (t.isContentEditable || t.tagName === "TEXTAREA") return;
        if (t.tagName === "INPUT" && t !== scanInput && t !== locationInput)
          return;
      }
      const now = performance.now();
      if (event.key === "Enter") {
        // Only a terminator that follows its own payload closely is this
        // scan's; anything else (a stale buffer, a scanner's extra CR+LF)
        // submits nothing. Stray Enters are still swallowed while the dialog
        // is open, where they would "click" its focused close button and make
        // the dialog vanish unseen; elsewhere plain Enter reaches forms and
        // buttons untouched.
        const terminates = buffer && now - lastKeyAt <= TERMINATOR_MS;
        if (!terminates) {
          if (buffer) {
            buffer = "";
            render();
          }
          if (dialog.open) event.preventDefault();
          return;
        }
        event.preventDefault();
        clearTimeout(idleTimer);
        const code = buffer.trim();
        buffer = "";
        render();
        if (dialog.open) handleLocationScan(code);
        else handleBagScan(code);
        return;
      }
      if (event.key === "Backspace" && buffer) {
        event.preventDefault();
        lastKeyAt = now;
        buffer = buffer.slice(0, -1);
        render();
        armIdleReset();
        return;
      }
      if (event.key.length !== 1) return; // printable characters only
      const deliberate = now - lastKeyAt > BURST_GAP_MS && isPageControl(t);
      lastKeyAt = now;
      buffer += event.key;
      render();
      armIdleReset();
      if (!deliberate) event.preventDefault();
    },
    true,
  );

  // A scan can't reach this page while ANOTHER OS window holds focus — which
  // looks exactly like a dead field, because the browser keeps the focus ring
  // painted in an unfocused window. Say so loudly instead of staying silent.
  // It only BORROWS the message slot: the genuine message is repainted on
  // return, so an alt-tab round trip can't erase an error the user has not
  // acted on yet ("Unknown location code SL777 — reprint the label?").
  let blurWarned = false;
  window.addEventListener("blur", () => {
    blurWarned = true;
    const message =
      "This window is not focused — scans are going elsewhere. " +
      "Click the page (or alt-tab back) to resume.";
    if (dialog.open) paintDialogError(message);
    else paintStatus(message, "error");
  });
  window.addEventListener("focus", () => {
    if (!blurWarned) return;
    blurWarned = false;
    paintDialogError(dialogError);
    paintStatus(statusText, statusTone);
  });

  // Manual fallback for a torn or missing label: pick from the select (by
  // mouse) and Save.
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!locationSelect.value) {
      errorEl.textContent = "Scan a location label or pick one from the list.";
      errorEl.hidden = false;
      return;
    }
    saveLocation(Number(locationSelect.value));
  });

  // However the dialog closes (save, Cancel, Escape), the collector switches
  // back to the bag field — state, not focus, decides where scans go — and a
  // bag scanned a beat too early gets its turn.
  dialog.addEventListener("close", () => {
    target = null;
    buffer = "";
    render();
    drainQueue();
  });

  render(); // arm the bag field from the start
})();

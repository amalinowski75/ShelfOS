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

  // Give up on a hung request rather than holding `busy` forever — a stuck
  // flag would silently eat every scan that follows.
  const FETCH_TIMEOUT_MS = 10_000;

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

  function setStatus(message, tone) {
    statusEl.textContent = message;
    statusEl.className = tone === "error" ? "error" : "muted";
    statusEl.hidden = !message;
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
    errorEl.hidden = true;
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
      const keys = new Set(
        [parsed.mpn, parsed.distributor_pn].filter(Boolean).map(norm),
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
    const matched = SL.exec(code);
    if (!matched) {
      errorEl.textContent =
        "That is not a location label — scan the SL… code on the shelf.";
      errorEl.hidden = false;
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
    const path = LOCATIONS.get(String(locationId));
    if (!path) {
      errorEl.textContent = `Unknown location code SL${locationId} — reprint the label?`;
      errorEl.hidden = false;
      return;
    }
    busy = true;
    try {
      const url =
        target.kind === "import"
          ? `/api/invoices/${invoiceId}/import-lines/${target.row.dataset.importLineId}`
          : `/api/invoices/${invoiceId}/lines/${target.row.dataset.lineId}/location`;
      const resp = await fetch(url, {
        method: target.kind === "import" ? "PATCH" : "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ location_id: locationId }),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!resp.ok) {
        errorEl.textContent = await errorMessage(resp);
        errorEl.hidden = false;
        return;
      }
      applyToRow(target, locationId, path);
      const label = rowLabel(target.row);
      target = null;
      dialog.close();
      showToast(`${label} → ${path}`, { tone: "ok" });
    } catch {
      errorEl.textContent = "Could not reach the server.";
      errorEl.hidden = false;
    } finally {
      busy = false;
    }
  }

  // The collector. Capture phase on the document, so it sees every keystroke
  // before any control can swallow it, wherever focus happens to rest. It
  // stands down only where the user is genuinely typing: any OTHER open dialog
  // owns its keys, and so do free-text areas and inputs that aren't ours.
  // (The putaway dialog's manual select stays mouse-operated — letting it keep
  // keyboard behaviour would reopen the focus-dependence this design removes.)
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
      if (event.key === "Enter") {
        if (!buffer) {
          // A scanner's extra terminator (CR+LF, double CR) arrives right
          // after the code's own Enter — with the dialog just opened and its
          // close button holding focus, letting it through "clicks" that
          // button and the dialog vanishes before the user ever sees it.
          // Swallow stray Enters while the dialog is up; elsewhere plain
          // Enter keeps reaching forms and buttons untouched.
          if (dialog.open) event.preventDefault();
          return;
        }
        event.preventDefault();
        const code = buffer.trim();
        buffer = "";
        render();
        if (dialog.open) handleLocationScan(code);
        else handleBagScan(code);
        return;
      }
      if (event.key === "Backspace" && buffer) {
        event.preventDefault();
        buffer = buffer.slice(0, -1);
        render();
        return;
      }
      if (event.key.length !== 1) return; // printable characters only
      event.preventDefault();
      buffer += event.key;
      render();
    },
    true,
  );

  // A scan can't reach this page while ANOTHER OS window holds focus — which
  // looks exactly like a dead field, because the browser keeps the focus ring
  // painted in an unfocused window. Say so loudly instead of staying silent.
  let blurWarned = false;
  window.addEventListener("blur", () => {
    blurWarned = true;
    const message =
      "This window is not focused — scans are going elsewhere. " +
      "Click the page (or alt-tab back) to resume.";
    if (dialog.open) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    } else {
      setStatus(message, "error");
    }
  });
  window.addEventListener("focus", () => {
    if (!blurWarned) return;
    blurWarned = false;
    setStatus("");
    errorEl.hidden = true;
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

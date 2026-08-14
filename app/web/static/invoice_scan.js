// Scan-driven putaway on a draft invoice: scan a bag's barcode into the panel
// field → the matching line's "Set location" dialog opens; scan an SL<id>
// location label into it → the location is saved, the dialog closes and focus
// returns to the panel for the next bag. Uses shared.js helpers (csrfToken,
// errorMessage, showToast). The wedge scanner is a keyboard, so every step is
// "focused input + Enter" — no camera, no special APIs.
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

  const norm = (value) => (value || "").trim().toUpperCase();

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
    locationInput.value = "";
    locationSelect.value =
      match.kind === "import"
        ? match.row.querySelector(".ril-location")?.value || ""
        : match.row.dataset.locationId || "";
    errorEl.hidden = true;
    dialog.showModal();
    locationInput.focus();
  }

  async function handleBagScan() {
    const code = scanInput.value.trim();
    scanInput.value = "";
    if (!code || busy) return;
    if (SL.test(code)) {
      setStatus("That is a location label — scan a bag first.", "error");
      return;
    }
    busy = true;
    setStatus("Reading…");
    try {
      const resp = await fetch("/api/shops/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ code }),
      });
      if (!resp.ok) {
        setStatus(await errorMessage(resp), "error");
        return;
      }
      const parsed = await resp.json();
      const keys = new Set(
        [parsed.mpn, parsed.distributor_pn].filter(Boolean).map(norm),
      );
      if (!keys.size) {
        setStatus("No part number in this code.", "error");
        return;
      }
      const match = findRow(keys);
      if (!match) {
        setStatus(`No line on this invoice matches ${[...keys].join(" / ")}.`, "error");
        return;
      }
      setStatus("");
      openPutaway(match);
    } catch {
      setStatus("Could not reach the server.", "error");
    } finally {
      busy = false;
    }
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

  // The wedge scanner terminates its payload with Enter.
  scanInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    handleBagScan();
  });

  // A scan fired with focus anywhere else on the page (the natural mistake —
  // hands are on the scanner, not the mouse) must not be lost: route the first
  // printable keystroke into the scan field; the rest of the payload follows it
  // there. Real typing targets (inputs, selects, textareas) are left alone.
  document.addEventListener("keydown", (event) => {
    if (dialog.open) return; // the dialog manages its own focus
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.key.length !== 1) return; // printable characters only
    const t = event.target;
    if (
      t instanceof HTMLElement &&
      (t.isContentEditable ||
        t.tagName === "INPUT" ||
        t.tagName === "TEXTAREA" ||
        t.tagName === "SELECT")
    )
      return;
    scanInput.focus(); // the character lands in the field after the focus shift
  });

  locationInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const code = locationInput.value.trim();
    locationInput.value = "";
    const matched = SL.exec(code);
    if (!matched) {
      errorEl.textContent =
        "That is not a location label — scan the SL… code on the shelf.";
      errorEl.hidden = false;
      return;
    }
    saveLocation(Number(matched[1]));
  });

  // Manual fallback for a torn or missing label: pick from the select and Save.
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!locationSelect.value) {
      errorEl.textContent = "Scan a location label or pick one from the list.";
      errorEl.hidden = false;
      return;
    }
    saveLocation(Number(locationSelect.value));
  });

  // However the dialog closes (save, Cancel, Escape), the next bag scan should
  // land in the panel without the user reaching for the mouse.
  dialog.addEventListener("close", () => {
    target = null;
    scanInput.focus();
  });
})();

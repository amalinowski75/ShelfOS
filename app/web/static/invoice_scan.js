// Scan putaway on a draft invoice: a scanned bag resolves to the invoice line
// that ordered it, and saving files that line's location. The scanning itself
// (collector, dialog, all the hard-won keyboard behaviour) lives in
// scan_putaway.js, which this adapter configures.
(() => {
  const detail = document.getElementById("invoice-detail");
  if (!detail || !document.getElementById("scan-panel")) return;
  const invoiceId = detail.dataset.invoiceId;

  const norm = (value) => (value || "").trim().toUpperCase();

  function rowLabel(row) {
    return (
      row.dataset.mpn ||
      row.dataset.spn ||
      row.querySelector("a, .mono")?.textContent.trim() ||
      "line"
    );
  }

  function currentLocation(match) {
    if (match.kind === "import")
      return match.row.querySelector(".ril-location")?.value || "";
    return match.row.dataset.locationId || "";
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
    return matches.find((m) => !currentLocation(m)) || matches[0] || null;
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

  window.initScanPutaway({
    async resolve(code) {
      const resp = await scanFetch("/api/shops/parse", "POST", { code });
      if (!resp.ok) throw new ScanMiss(await errorMessage(resp));
      const parsed = await resp.json();
      // A QR that is only the product URL still has an identifier: the shop's
      // own symbol, somewhere in the path. Strictly a FALLBACK — the path also
      // yields category/manufacturer segments, and letting those compete with
      // a real part number could file the bag against the wrong line.
      const identifiers = [parsed.mpn, parsed.distributor_pn].filter(Boolean);
      const keys = new Set(
        (identifiers.length ? identifiers : parsed.url_symbols || []).map(norm),
      );
      if (!keys.size) throw new ScanMiss("No part number in this code.");
      const match = findRow(keys);
      if (!match) {
        throw new ScanMiss(
          `No line on this invoice matches ${[...keys].join(" / ")}.`,
        );
      }
      return {
        label: rowLabel(match.row),
        description: match.row.dataset.description || "",
        locationId: currentLocation(match) || null,
        async save(locationId, path) {
          const url =
            match.kind === "import"
              ? `/api/invoices/${invoiceId}/import-lines/${match.row.dataset.importLineId}`
              : `/api/invoices/${invoiceId}/lines/${match.row.dataset.lineId}/location`;
          const saved = await scanFetch(
            url,
            match.kind === "import" ? "PATCH" : "PUT",
            { location_id: locationId },
          );
          if (!saved.ok) throw new ScanMiss(await errorMessage(saved));
          applyToRow(match, locationId, path);
        },
      };
    },
  });
})();

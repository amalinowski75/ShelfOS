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

  // Reflect what the server has accepted. Either half may be null, so the
  // count can be applied the moment its own write lands, before the location's.
  function applyToRow(match, locationId, path, quantity) {
    if (quantity != null) {
      match.row.dataset.quantity = String(quantity);
      const qtyCell =
        match.kind === "import"
          ? match.row.querySelector("td.num")
          : match.row.children[3];
      if (qtyCell) qtyCell.textContent = String(quantity);
    }
    if (locationId == null) return;
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
      // Every number the label states outright, because an invoice line can
      // carry either of them: a TME bag prints BOTH the shop's ordering symbol
      // (PN: → parsed.mpn, matching a line's supplier_part_number) and the
      // manufacturer's own (MPN: → parsed.manufacturer_pn, matching the
      // component's mpn). Which one the line has to offer is not the scanner's
      // business — TME's PDF truncates its symbol column where it wraps, so the
      // manufacturer's number is sometimes the only one the row carries.
      //
      // A QR that is only the product URL still has an identifier: the shop's
      // own symbol, somewhere in the path. Strictly a FALLBACK — the path also
      // yields category/manufacturer segments, and letting those compete with
      // a real part number could file the bag against the wrong line.
      const identifiers = [
        parsed.manufacturer_pn,
        parsed.mpn,
        parsed.distributor_pn,
      ].filter(Boolean);
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
      const invoiced = Number(match.row.dataset.quantity) || 1;
      return {
        label: rowLabel(match.row),
        description: match.row.dataset.description || "",
        locationId: currentLocation(match) || null,
        // What the invoice says arrived. A line holds ONE quantity, so editing
        // this corrects the line itself — for the bag that came up short.
        quantity: invoiced,
        quantityHint: `as invoiced — change it if the bag holds a different count`,
        async save(locationId, path, quantity) {
          const recounted = quantity !== invoiced;
          if (match.kind === "import") {
            const body = { location_id: locationId };
            if (recounted) body.quantity = quantity;
            const saved = await scanFetch(
              `/api/invoices/${invoiceId}/import-lines/${match.row.dataset.importLineId}`,
              "PATCH",
              body,
            );
            if (!saved.ok) throw new ScanMiss(await errorMessage(saved));
          } else {
            // Two writes, because a line's quantity and its location have
            // separate endpoints. The count goes first and is applied to the
            // row as soon as it lands, so a failure of the second one leaves
            // the page telling the truth about the first — and a rescan reads
            // the new count rather than re-sending the old one forever.
            const lineId = match.row.dataset.lineId;
            if (recounted) {
              const requantified = await scanFetch(
                `/api/invoices/${invoiceId}/lines/${lineId}`,
                "PUT",
                { quantity },
              );
              if (!requantified.ok) {
                throw new ScanMiss(await errorMessage(requantified));
              }
              applyToRow(match, null, null, quantity);
            }
            const saved = await scanFetch(
              `/api/invoices/${invoiceId}/lines/${lineId}/location`,
              "PUT",
              { location_id: locationId },
            );
            if (!saved.ok) {
              const why = await errorMessage(saved);
              throw new ScanMiss(
                recounted
                  ? `Quantity saved as ${quantity}, but the location could not be set: ${why}`
                  : why,
              );
            }
          }
          applyToRow(match, locationId, path, quantity);
          if (recounted) {
            // The row's Total and the invoice's Total net are computed
            // server-side from the quantity, so they are now stale on screen.
            // Re-render rather than recompute money in the browser.
            window.location.reload();
          }
        },
      };
    },
  });
})();

// Scan putaway on the components page: a scanned bag resolves to the component
// it holds, and saving RELOCATES that component's stock to the scanned shelf.
// The scanning itself lives in scan_putaway.js, which this adapter configures.
//
// "Set location" means something different here than on an invoice: a component
// has no location field, its stock does. So a save moves every unit out of the
// one place that holds it and into the scanned one. Two situations can't be
// answered by a single scan and say so instead of guessing: stock in no place
// at all (nothing to move — use Add stock), and stock split across several
// (which pile was in your hand?).
(() => {
  if (!document.getElementById("scan-panel")) return;

  function describe(component) {
    return [component.manufacturer, component.description]
      .filter(Boolean)
      .join(" · ");
  }

  window.initScanPutaway({
    async resolve(code) {
      const resp = await scanFetch("/api/components/scan", "POST", { code });
      if (!resp.ok) throw new ScanMiss(await errorMessage(resp));
      const { identifiers, matches } = await resp.json();
      const seen = identifiers.join(" / ") || "this code";
      if (!matches.length) {
        // Nothing in the inventory holds this code yet. Instead of a dead-end
        // miss, open the New Component dialog and hand it the scanned code: its
        // import field looks the part up and pre-fills the form, so a brand-new
        // bag goes straight from scan to create without rescanning. Returning
        // nothing tells scan_putaway the scan was taken care of here.
        if (window.openComponentDialog) {
          // Keep the diagnostic the miss used to carry: name what was scanned, so
          // if the user cancels (or the shop lookup can't resolve it either) the
          // scan isn't left with no trace on the now-blank panel.
          showToast(`${seen} isn't in the inventory yet — creating it.`, {
            tone: "ok",
          });
          window.openComponentDialog(
            (created) => {
              window.location = `/components/${created.id}`;
            },
            null,
            { importCode: code },
          );
          return null;
        }
        throw new ScanMiss(`No component matches ${seen}.`);
      }
      if (matches.length > 1) {
        throw new ScanMiss(
          `${matches.length} components share ${seen} — move it from its own page.`,
        );
      }
      const component = matches[0];
      const held = component.locations;
      if (!held.length) {
        throw new ScanMiss(
          `${component.mpn || "That component"} has no stock recorded — ` +
            "use Add stock to put it somewhere first.",
        );
      }
      if (held.length > 1) {
        const where = held.map((l) => `${l.path} (${l.quantity})`).join(", ");
        throw new ScanMiss(
          `${component.mpn || "That component"} is stocked in several places ` +
            `(${where}) — move it from its own page.`,
        );
      }
      const source = held[0];
      return {
        label: component.mpn || `Component #${component.id}`,
        description: describe(component),
        locationId: source.id,
        // The whole slot, because a bag usually moves whole — but editable for
        // the times only part of it does.
        quantity: source.quantity,
        maxQuantity: source.quantity,
        quantityHint: `of ${source.quantity} in ${source.path}`,
        async save(locationId, path, quantity) {
          if (locationId === source.id) {
            // Nothing to do — but say so instead of showing a green "moved"
            // toast for a move that never happened. A typed count makes it a
            // refusal: it states an intent this no-op cannot satisfy.
            throw new ScanMiss(
              quantity === source.quantity
                ? `Already in ${path} — nothing moved.`
                : `Already in ${path} — the ${quantity} you typed went nowhere. ` +
                  "Scan the shelf it should move to.",
            );
          }
          const moved = await scanFetch("/api/stock/move", "POST", {
            component_id: component.id,
            from_location_id: source.id,
            to_location_id: locationId,
            quantity,
          });
          if (!moved.ok) throw new ScanMiss(await errorMessage(moved));
          // The table shows totals, not places, so a move leaves it accurate —
          // nothing to refresh.
        },
      };
    },
  });
})();

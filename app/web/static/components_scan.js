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
      if (!matches.length) throw new ScanMiss(`No component matches ${seen}.`);
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
        async save(locationId) {
          if (locationId === source.id) return; // already there; nothing to move
          const moved = await scanFetch("/api/stock/move", "POST", {
            component_id: component.id,
            from_location_id: source.id,
            to_location_id: locationId,
          });
          if (!moved.ok) throw new ScanMiss(await errorMessage(moved));
          // The table shows totals, not places, so a move leaves it accurate —
          // nothing to refresh.
        },
      };
    },
  });
})();

// What "move stock" means, wherever it is offered. A component has no location
// field — its stock does — so a move takes a count out of one shelf and puts it
// on another, and the second half of that is a scan: the shelf you are standing
// at, not a path hunted for in a list.
//
// This builds the TARGET the putaway dialog files (see scan_putaway.js). Two
// surfaces hand it one: the components page, after a scanned bag asks what it is
// for (components_scan.js), and a component's own page, from a "Stock by
// location" row (component_move.js). They differ only in what they know up front
// and what they do once the move lands (`onMoved`) — the refusal of a move to
// where the stock already is, and the write itself, are the same on both, and
// belong in one place.
//
// `scanFetch`, `ScanMiss` and `errorMessage` come from scan_putaway.js and
// shared.js, so this file loads after them.
window.moveStockTarget = function ({
  componentId,
  label,
  description,
  sources,
  onMoved,
}) {
  return {
    title: "Move stock",
    label,
    description,
    // Deliberately empty: with several piles in play a prefilled "to" is just a
    // wrong guess wearing a confident face.
    locationId: null,
    quantity: null, // the chosen source sets the count and its ceiling
    maxQuantity: null,
    quantityHint: "",
    sources,
    async save(locationId, path, quantity, fromId) {
      if (locationId === fromId) {
        // Nothing to do — but say so instead of showing a green "moved" toast
        // for a move that never happened. A count smaller than the pile makes
        // it a refusal: it states an intent this no-op cannot satisfy.
        const whole = sources.find((s) => s.id === fromId)?.quantity;
        throw new ScanMiss(
          quantity === whole
            ? `Already in ${path} — nothing moved.`
            : `Already in ${path} — the ${quantity} you typed went nowhere. ` +
              "Scan the shelf it should move to.",
        );
      }
      const moved = await scanFetch("/api/stock/move", "POST", {
        component_id: componentId,
        from_location_id: fromId,
        to_location_id: locationId,
        quantity,
      });
      if (!moved.ok) throw new ScanMiss(await errorMessage(moved));
      await onMoved?.();
    },
  };
};

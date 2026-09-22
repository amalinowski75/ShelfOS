// Move stock from a component's own page: every "Stock by location" row carries
// a Move button, and it opens the same putaway dialog the components page uses,
// so the destination shelf is SCANNED rather than picked out of a list.
//
// Before this, moving a bag from its own page meant Take here, then Add there —
// two writes, two dialogs, and a ledger that records a disappearance and an
// appearance instead of a move.
//
// The dialog's first step (scan the bag to find out what it is) has no work to do
// here: the page already is that component, and the row says which pile is
// moving. So this page renders the dialog without the scan panel and files a
// target straight into it — see the header of scan_putaway.js for what that
// leaves running. The move itself is stock_move.js's, shared with the scan flow.
(() => {
  const table = document.getElementById("stock-locations");
  const buttons = [...(table?.querySelectorAll("[data-move-from]") ?? [])];
  if (!buttons.length) return; // read-only viewer, retired part, or nothing in stock

  const scan = window.initScanPutaway();
  if (!scan) return; // no dialog rendered — nothing to open

  // A reload is a PENDING navigation: the page stays live and clickable until the
  // server answers, and a second move posted in that window would be filed
  // against quantities that are already history. Same latch (and the same
  // pageshow release, for a stopped or bfcache-restored load) as stock_dialog.js.
  let navigating = false;
  window.addEventListener("pageshow", () => {
    navigating = false;
  });

  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (navigating) return;
      scan.fileTarget(
        window.moveStockTarget({
          componentId: Number(table.dataset.componentId),
          label: table.dataset.label,
          description: table.dataset.description || "",
          // One source, this row's own pile: the button already said which shelf
          // the stock is coming off, so there is nothing left to choose. The
          // dialog still names it, and caps the count at what the pile holds.
          sources: [
            {
              id: Number(button.dataset.moveFrom),
              path: button.dataset.path,
              quantity: Number(button.dataset.quantity),
            },
          ],
          onMoved: () => {
            // Both tables a move changes — stock by location and the movement
            // ledger — were rendered by Jinja, so only a reload can show it.
            navigating = true;
            window.location.reload();
          },
        }),
      );
    });
  }
})();

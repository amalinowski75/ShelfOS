// Locations page (spec §7): a collapsible storage tree that also shows what each
// location holds, plus the two "Show empty" / "Show occupied" filters.
//
// The tree used to render fully expanded, which is unreadable once there are more
// than a handful of locations. Everything starts collapsed here; the filters expand
// the paths to whatever they match, so a narrowed view still needs no navigation.

(function () {
  const tree = document.getElementById("location-tree");
  if (!tree) return; // no locations yet
  const emptyHint = document.getElementById("location-tree-empty");
  const showEmpty = document.getElementById("show-empty");
  const showOccupied = document.getElementById("show-occupied");

  const branchOf = (item) => item.querySelector(":scope > .tree-children");
  const caretOf = (item) => item.querySelector(":scope > .loc-row > .tree-caret");
  const partsOf = (item) => branchOf(item)?.querySelector(":scope > .loc-parts");
  const countOf = (item) => item.querySelector(":scope > .loc-row > .loc-count");

  function setExpanded(item, open) {
    const branch = branchOf(item);
    if (!branch) return;
    branch.hidden = !open;
    caretOf(item)?.setAttribute("aria-expanded", String(open));
  }

  tree.addEventListener("click", (event) => {
    const caret = event.target.closest(".tree-caret");
    if (!caret || !tree.contains(caret)) return;
    // The button's own state, not the branch's: it is what the user reads and
    // what a screen reader announces, so it stays the single source of truth.
    const open = caret.getAttribute("aria-expanded") === "true";
    setExpanded(caret.closest(".loc-item"), !open);
  });

  function applyFilters() {
    const empty = showEmpty.checked;
    const occupied = showOccupied.checked;
    // Both ticked is "no filter": leave the user's own expand/collapse alone
    // rather than blowing the whole tree open again.
    const filtering = !(empty && occupied);

    if (!empty && !occupied) {
      // Nothing to show. Return WITHOUT walking, so the expansion the user built
      // up survives ticking a box off and on again.
      tree.hidden = true;
      if (emptyHint) emptyHint.hidden = false;
      return;
    }

    // Returns true when this list still has something visible in it.
    function walk(list) {
      let anyVisible = false;
      for (const item of list.querySelectorAll(":scope > .loc-item")) {
        const branch = branchOf(item);
        const parts = partsOf(item);
        const childList = branch?.querySelector(":scope > .loc-tree");
        const hasVisibleChild = childList ? walk(childList) : false;
        const self = item.dataset.occupied === "true" ? occupied : empty;

        // The contents and the count belong to THIS location, so they follow its
        // own match — not the branch's expansion. Otherwise a location kept only
        // as the path to a match below would still list the very parts the filter
        // was asked to hide.
        const ownHidden = filtering && !self;
        if (parts) parts.hidden = ownHidden;
        const count = countOf(item);
        if (count) count.hidden = ownHidden;

        // A location that doesn't match is still the only way to reach a match
        // beneath it, so it stays on screen — marked as scaffolding, not a hit.
        item.hidden = !(self || hasVisibleChild);
        item.classList.toggle("is-path", filtering && !self && hasVisibleChild);
        if (filtering && branch) {
          // Open when there is something below worth reading: a matching
          // descendant, or this location's own contents when it matched. Without
          // the second half, filtering to "occupied" would show every full drawer
          // shut — the one thing the filter was used to find.
          setExpanded(item, hasVisibleChild || (self && !!parts));
        }
        anyVisible = anyVisible || !item.hidden;
      }
      return anyVisible;
    }

    const visible = walk(tree);
    if (emptyHint) emptyHint.hidden = visible;
    tree.hidden = !visible;
  }

  showEmpty.addEventListener("change", applyFilters);
  showOccupied.addEventListener("change", applyFilters);
  applyFilters();
})();

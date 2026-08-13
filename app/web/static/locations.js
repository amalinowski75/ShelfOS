// Locations page (spec §7): a collapsible storage tree that also shows what each
// location holds, plus the two "Show empty" / "Show occupied" filters.
//
// The tree used to render fully expanded, which is unreadable once there are more
// than a handful of locations. Everything starts collapsed here; the filters expand
// the paths to whatever they match, so a narrowed view still needs no navigation.

(function () {
  const tree = document.getElementById("location-tree");
  const emptyHint = document.getElementById("location-tree-empty");
  const showEmpty = document.getElementById("show-empty");
  const showOccupied = document.getElementById("show-occupied");
  // All three or none: the template gates them on the same condition (there being
  // any locations), but relying on two conditions happening to agree would fail
  // as a TypeError that takes the whole page's filtering out on load.
  if (!tree || !showEmpty || !showOccupied) return;

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

  // The expansion the user built up, keyed by location id and kept in
  // sessionStorage: every edit/delete/create reloads the page, and losing the
  // whole unfolded tree to each small change made editing miserable. Session-
  // scoped on purpose — a fresh visit still starts collapsed. Only the USER's
  // own clicks are remembered; the filters' auto-expansion stays transient.
  const EXPANDED_KEY = "shelfos-locations-expanded";
  let expandedIds;
  try {
    expandedIds = new Set(JSON.parse(sessionStorage.getItem(EXPANDED_KEY)) || []);
  } catch {
    expandedIds = new Set();
  }

  function rememberExpansion(id, open) {
    if (!id) return;
    if (open) expandedIds.add(id);
    else expandedIds.delete(id);
    try {
      sessionStorage.setItem(EXPANDED_KEY, JSON.stringify([...expandedIds]));
    } catch {
      /* storage full/blocked — the tree still works, it just won't remember */
    }
  }

  if (expandedIds.size) {
    for (const item of tree.querySelectorAll(".loc-item[data-id]")) {
      if (expandedIds.has(item.dataset.id)) setExpanded(item, true);
    }
  }

  tree.addEventListener("click", (event) => {
    const caret = event.target.closest(".tree-caret");
    if (!caret || !tree.contains(caret)) return;
    // The button's own state, not the branch's: it is what the user reads and
    // what a screen reader announces, so it stays the single source of truth.
    const open = caret.getAttribute("aria-expanded") === "true";
    const item = caret.closest(".loc-item");
    setExpanded(item, !open);
    rememberExpansion(item.dataset.id, !open);
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
      say("Nothing to show — tick “Show empty” or “Show occupied”.");
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
    tree.hidden = !visible;
    // Getting here with nothing visible means exactly one box is ticked (with
    // both, every location matches one of them). So name what came up empty
    // rather than telling the user to tick a box they already have ticked.
    say(visible ? null : occupied ? "No occupied locations." : "No empty locations.");
  }

  function say(message) {
    if (!emptyHint) return;
    if (message) emptyHint.textContent = message;
    emptyHint.hidden = !message;
  }

  showEmpty.addEventListener("change", applyFilters);
  showOccupied.addEventListener("change", applyFilters);
  applyFilters();

  // ----- per-node Edit / Delete actions (writers only; buttons absent otherwise)

  // The full path ("Lab / Rack A / Shelf 1"), read off the DOM nesting, so the
  // delete confirm names exactly what is about to go.
  function pathOf(item) {
    const parts = [];
    for (let li = item; li; li = li.parentElement?.closest(".loc-item")) {
      const name = li.querySelector(":scope > .loc-row .loc-name");
      if (name) parts.unshift(name.textContent.trim());
    }
    return parts.join(" / ");
  }

  // One delete at a time: a double-click would otherwise fire two DELETEs and
  // surface a spurious "not found" toast for the second.
  let deleting = false;

  tree.addEventListener("click", async (event) => {
    const addBtn = event.target.closest(".loc-add");
    const editBtn = event.target.closest(".loc-edit");
    const deleteBtn = event.target.closest(".loc-delete");
    if ((!addBtn && !editBtn && !deleteBtn) || !tree.contains(event.target))
      return;
    const item = event.target.closest(".loc-item");
    if (!item) return;
    const id = Number(item.dataset.id);

    if (addBtn) {
      // Expand this node in the remembered state before the reload, so the
      // just-created child is on screen instead of behind a collapsed caret.
      openLocationDialog(
        () => {
          rememberExpansion(item.dataset.id, true);
          window.location.reload();
        },
        null,
        { parentId: id },
      );
      return;
    }

    if (editBtn) {
      // A location may not become its own parent nor land inside its own
      // subtree: bar itself and every descendant in the dialog's parent select.
      const disabledIds = [id];
      for (const child of item.querySelectorAll(".loc-item[data-id]"))
        disabledIds.push(Number(child.dataset.id));
      openLocationDialog(() => window.location.reload(), {
        id,
        name:
          item
            .querySelector(":scope > .loc-row .loc-name")
            ?.textContent.trim() ?? "",
        type: item.dataset.type,
        parentId: item.dataset.parentId ? Number(item.dataset.parentId) : null,
        disabledIds,
      });
      return;
    }

    if (deleting) return;
    // A branch goes down with everything under it — the confirm must say how
    // much that is, not just name the top. Stock anywhere in the branch still
    // blocks server-side, so "everything" can only be empty locations.
    const descendants = item.querySelectorAll(".loc-item").length;
    const message =
      descendants > 0
        ? `Delete location "${pathOf(item)}" AND the ${descendants} ` +
          `location${descendants === 1 ? "" : "s"} under it?`
        : `Delete location "${pathOf(item)}"?`;
    if (!window.confirm(message)) return;
    deleting = true;
    try {
      let resp;
      try {
        resp = await fetch(
          `/api/locations/${id}${descendants > 0 ? "?recursive=true" : ""}`,
          {
            method: "DELETE",
            headers: { "X-CSRF-Token": csrfToken },
          },
        );
      } catch {
        showToast("Could not reach the server. Please try again.");
        return;
      }
      if (!resp.ok) {
        showToast(await errorMessage(resp));
        return;
      }
      window.location.reload();
    } finally {
      deleting = false;
    }
  });
})();

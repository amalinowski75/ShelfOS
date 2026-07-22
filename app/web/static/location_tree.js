// Expandable location tree-picker (spec §7). Enhances every `.loc-picker`
// widget on the page: a toggle button opens a dropdown holding the location
// tree; branches expand/collapse; picking a node writes its id into the hidden
// input the form already reads. Each picker element also gets `.setValue(id)`
// and `.reset()` for programmatic control (e.g. opening a dialog with a value).

(function () {
  for (const picker of document.querySelectorAll(".loc-picker")) {
    enhanceLocationPicker(picker);
  }

  function enhanceLocationPicker(picker) {
    const input = picker.querySelector('input[type="hidden"]');
    const toggle = picker.querySelector(".loc-picker-toggle");
    const label = picker.querySelector(".loc-picker-label");
    const menu = picker.querySelector(".loc-picker-menu");
    const placeholder = label.textContent;

    const close = () => {
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
    };

    function collapseAll() {
      for (const branch of picker.querySelectorAll(".loc-picker-children")) {
        branch.hidden = true;
      }
      for (const caret of picker.querySelectorAll(".loc-picker-caret")) {
        caret.setAttribute("aria-expanded", "false");
      }
    }

    function selectNode(id, path) {
      input.value = id || "";
      label.textContent = input.value ? path : placeholder;
    }

    // Match by data attribute rather than building a selector string, so an
    // arbitrary caller value can't produce a malformed query.
    function nodeById(id) {
      const value = String(id);
      return [...menu.querySelectorAll(".loc-picker-node")].find(
        (candidate) => candidate.dataset.locId === value,
      );
    }

    toggle.setAttribute("aria-haspopup", "true");
    toggle.setAttribute("aria-expanded", "false");

    const setOpen = (open) => {
      menu.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
    };

    toggle.addEventListener("click", () => setOpen(menu.hidden));

    // Escape closes just the menu, not the enclosing <dialog>. preventDefault is
    // required: a native dialog's Escape-to-close is a default action that
    // stopPropagation alone does not cancel.
    picker.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !menu.hidden) {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
        toggle.focus();
      }
    });

    menu.addEventListener("click", (event) => {
      const caret = event.target.closest(".loc-picker-caret");
      if (caret) {
        const branch = caret
          .closest("li")
          .querySelector(":scope > .loc-picker-children");
        if (branch) {
          branch.hidden = !branch.hidden;
          caret.setAttribute("aria-expanded", String(!branch.hidden));
        }
        return;
      }
      const node = event.target.closest(".loc-picker-node");
      if (node) {
        selectNode(node.dataset.locId, node.dataset.locPath);
        close();
      }
    });

    // Close when clicking anywhere outside this picker.
    document.addEventListener("click", (event) => {
      if (!picker.contains(event.target)) close();
    });

    // Set the selection to a location id (or "" / null to clear), reflecting it
    // in the label; used when a dialog opens with an existing value.
    picker.setValue = (id) => {
      const value = id == null ? "" : String(id);
      const node = value ? nodeById(value) : null;
      // Clear rather than keep an id with no matching node (which would leave an
      // invalid value in the hidden input behind a blank label).
      if (value && !node) {
        selectNode("", "");
        return;
      }
      selectNode(value, node ? node.dataset.locPath : "");
    };

    picker.reset = () => {
      selectNode("", "");
      collapseAll();
      close();
      picker.setFilter(null);
    };

    // ---- caller-supplied filtering -----------------------------------------
    // `setFilter(fn)` narrows the tree to the ids `fn(id)` accepts; `setFilter(null)`
    // restores it. A node the filter rejects stays VISIBLE but unselectable when it
    // has an accepted descendant — otherwise the path to that descendant would be
    // unreachable in an expandable tree — and is hidden outright when it doesn't.
    // Matching branches are expanded, so a hit deep in the tree needs no navigation.
    const showAllWrap = picker.querySelector(".loc-picker-showall");
    const showAllBox = picker.querySelector(".loc-picker-showall-box");
    const noMatch = picker.querySelector(".loc-picker-nomatch");
    let accepts = null;
    // Locations created through "+ New location" while a filter is on: the user
    // just made this one on purpose, so it must be pickable whatever the filter says.
    let justCreated = new Set();

    function applyFilter() {
      const active = accepts && !showAllBox?.checked;
      const allowed = (node) =>
        !active ||
        justCreated.has(node.dataset.locId) ||
        accepts(Number(node.dataset.locId));

      // Returns true when this list contains anything still visible.
      function walk(list) {
        let anyVisible = false;
        for (const li of list.querySelectorAll(":scope > li")) {
          const node = li.querySelector(":scope > .loc-picker-row > .loc-picker-node");
          const branch = li.querySelector(":scope > .loc-picker-children");
          const sublist = branch?.querySelector(":scope > .loc-picker-list");
          const hasVisibleChild = sublist ? walk(sublist) : false;
          const self = node ? allowed(node) : false;
          if (node) {
            node.disabled = active && !self;
            node.classList.toggle("is-unavailable", active && !self);
          }
          li.hidden = !(self || hasVisibleChild);
          const caret = li.querySelector(":scope > .loc-picker-row > .loc-picker-caret");
          if (branch && active) {
            // Reveal the path to a match rather than making the user hunt for it.
            branch.hidden = !hasVisibleChild;
            caret?.setAttribute("aria-expanded", String(hasVisibleChild));
          }
          // An accepted node whose children were all filtered out keeps a caret
          // that expands to an empty box; hide it while the filter is on.
          if (caret) caret.hidden = active && !hasVisibleChild;
          anyVisible = anyVisible || !li.hidden;
        }
        return anyVisible;
      }

      let visible = false;
      for (const list of menu.querySelectorAll(":scope > .loc-picker-list")) {
        visible = walk(list) || visible;
      }
      // "— none —" on an optional picker is a real choice and sits outside the
      // tree, so it counts: otherwise "no matching locations" would appear right
      // above something perfectly pickable.
      const none = menu.querySelector(":scope > .loc-picker-none");
      if (none && !none.hidden) visible = true;
      if (noMatch) noMatch.hidden = !active || visible;

      // Drop a selection the filter has just taken away. Without this the toggle
      // keeps showing a location the tree no longer offers and the form still
      // POSTs it — reachable by picking before the filter lands, or by picking
      // under "show all" and then unticking it.
      const chosen = input.value ? nodeById(input.value) : null;
      if (chosen && (chosen.disabled || chosen.closest("li")?.hidden)) {
        selectNode("", "");
      }
    }

    showAllBox?.addEventListener("change", applyFilter);
    // The picker's only non-button control, and it sits inside the stock form:
    // browsers implicitly submit a form on Enter in a checkbox, so a keyboard user
    // reaching for it would post a stock movement. Toggle it instead.
    showAllBox?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      showAllBox.checked = !showAllBox.checked;
      applyFilter();
    });

    // While a caller is deciding what to offer, the full tree is live and a pick
    // made now would be silently dropped the moment the filter lands. Say so.
    picker.setBusy = (busy) => {
      toggle.disabled = !!busy;
      if (busy) {
        setOpen(false);
        label.textContent = "Loading locations…";
      } else if (!input.value) {
        label.textContent = placeholder;
      }
    };

    picker.setFilter = (fn) => {
      accepts = typeof fn === "function" ? fn : null;
      justCreated = new Set();
      if (showAllBox) showAllBox.checked = false;
      if (showAllWrap) showAllWrap.hidden = !accepts;
      applyFilter();
    };

    // Inline "+ New location": create a location without leaving this picker,
    // then insert it as a selectable entry and select it. Enhanced only when the
    // shared New Location dialog is present on the page (window.openLocationDialog);
    // the button stays hidden otherwise, so it never dangles without a handler.
    const newBtn = picker.querySelector(".loc-picker-new");
    if (newBtn && typeof window.openLocationDialog === "function") {
      newBtn.hidden = false;
      newBtn.addEventListener("click", () => {
        openLocationDialog(addCreatedLocation);
      });
    }

    function addCreatedLocation(created) {
      // The POST response carries no computed path, so build it from the parent
      // node already in the menu (falling back to the bare name at top level).
      let path = created.name;
      if (created.parent_id != null) {
        const parent = [...menu.querySelectorAll(".loc-picker-node")].find(
          (node) => node.dataset.locId === String(created.parent_id),
        );
        if (parent) path = `${parent.dataset.locPath} / ${created.name}`;
      }
      // Append a flat, path-labelled node so setValue can match it and a re-open
      // keeps it visible; the server-rendered tree nests it properly on the next
      // page load.
      let list = menu.querySelector(".loc-picker-list");
      if (!list) {
        menu.querySelector(".loc-picker-empty")?.remove();
        list = document.createElement("ul");
        list.className = "loc-picker-list";
        menu.appendChild(list);
      }
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "loc-picker-row";
      const spacer = document.createElement("span");
      spacer.className = "loc-picker-caret-spacer";
      const node = document.createElement("button");
      node.type = "button";
      node.className = "loc-picker-node";
      node.dataset.locId = String(created.id);
      node.dataset.locPath = path;
      node.textContent = path;
      if (created.type) {
        // Mirror the server-rendered nodes, which carry a type badge.
        const type = document.createElement("span");
        type.className = "loc-picker-type";
        type.textContent = created.type;
        node.appendChild(type);
      }
      row.append(spacer, node);
      li.append(row);
      list.appendChild(li);

      // Exempt it from any active filter — the user just created this location on
      // purpose, and a brand-new one can't satisfy a "holds this component" test.
      justCreated.add(String(created.id));
      applyFilter();

      picker.setValue(created.id);
      close();
    }
  }
})();

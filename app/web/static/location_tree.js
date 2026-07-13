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
      // Match by data attribute directly (no string-built selector), so an
      // arbitrary caller value can't produce a malformed query.
      const node = value
        ? [...menu.querySelectorAll(".loc-picker-node")].find(
            (candidate) => candidate.dataset.locId === value,
          )
        : null;
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

      picker.setValue(created.id);
      close();
    }
  }
})();

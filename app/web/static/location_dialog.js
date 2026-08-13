// Shared location dialog (spec §7). Loaded on every authenticated page; active
// only where the _location_dialog.html partial is present. Exposes
// `openLocationDialog(onDone, location?)` — without `location` it creates a new
// one, with `location` ({id, name, type, parentId, disabledIds}) it edits that
// location instead. `onDone` receives the created/updated location so each
// caller can react (reload the tree, select it in a picker…).
// Uses shared.js helpers (csrfToken, errorMessage).

(function () {
  const dialog = document.getElementById("location-dialog");
  if (!dialog) return; // page does not include the dialog

  const form = document.getElementById("location-form");
  const errorEl = document.getElementById("location-error");
  const titleEl = document.getElementById("location-dialog-title");
  const submitBtn = document.getElementById("location-submit");
  let onDone = null;
  // The location being edited ({id}), or null when creating.
  let editing = null;
  // Ignore re-entrant submits while a write is in flight, so a fast double-click
  // can't POST twice and leave a duplicate Location row behind.
  let submitting = false;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    try {
      const field = (name) => form.querySelector(`[name="${name}"]`);
      const parentSelect = field("parent_id");
      const parent = parentSelect.value;
      const name = field("name").value.trim();
      const body = JSON.stringify({
        type: field("type").value,
        name,
        parent_id: parent ? Number(parent) : null,
      });

      errorEl.hidden = true;
      let saved;
      try {
        const resp = await fetch(
          editing ? `/api/locations/${editing.id}` : "/api/locations",
          {
            method: editing ? "PATCH" : "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
            body,
          },
        );
        if (!resp.ok) {
          errorEl.textContent = await errorMessage(resp);
          errorEl.hidden = false;
          return;
        }
        saved = await resp.json();
      } catch {
        errorEl.textContent = "Could not reach the server. Please try again.";
        errorEl.hidden = false;
        return;
      }
      // Keep the parent <select> current so a just-created location can be a
      // parent right away — building Room → Rack → Bin without a page reload.
      // reset() (on the next open) clears the selection, not appended options.
      // Edits skip this: the edited location is already an option, and callers
      // reload anyway.
      if (!editing && !parentSelect.querySelector(`option[value="${saved.id}"]`)) {
        const parentPath = parent ? parentSelect.selectedOptions[0].text : "";
        const path = parentPath ? `${parentPath} / ${name}` : name;
        parentSelect.appendChild(new Option(path, saved.id));
      }
      dialog.close();
      // A caller's DOM update must not turn into an unhandled rejection (or leave
      // the write looking failed): the location is already persisted.
      if (onDone) {
        try {
          onDone(saved);
        } catch {
          /* swallow — the location was saved; only the caller's hook failed */
        }
      }
    } finally {
      submitting = false;
    }
  });

  // Open the dialog; `callback(saved)` runs after a successful create/edit.
  // `location` switches to edit mode: {id, name, type, parentId, disabledIds}
  // where `disabledIds` (self + descendants) are barred as the new parent —
  // a location cannot move under its own subtree.
  // `defaults` seeds create mode: {parentId} preselects the parent, so an
  // "add under this location" entry point needs no hunting in the select.
  window.openLocationDialog = function (callback, location, defaults) {
    onDone = callback || null;
    editing = location ? { id: location.id } : null;
    form.reset();
    errorEl.hidden = true;
    if (titleEl) titleEl.textContent = editing ? "Edit location" : "New location";
    if (submitBtn)
      submitBtn.textContent = editing ? "Save changes" : "Create location";
    const parentSelect = form.querySelector('[name="parent_id"]');
    for (const opt of parentSelect.options) opt.disabled = false;
    if (!location && defaults && defaults.parentId != null) {
      parentSelect.value = String(defaults.parentId);
    }
    if (location) {
      form.querySelector('[name="type"]').value = location.type;
      form.querySelector('[name="name"]').value = location.name;
      const blocked = new Set((location.disabledIds || [location.id]).map(String));
      for (const opt of parentSelect.options) {
        if (blocked.has(opt.value)) opt.disabled = true;
      }
      parentSelect.value =
        location.parentId != null ? String(location.parentId) : "";
    }
    dialog.showModal();
  };

  // Standalone trigger on the Locations page: create, then reload so the tree
  // re-renders with the new location.
  const newBtn = document.getElementById("new-location-btn");
  if (newBtn) {
    newBtn.addEventListener("click", () =>
      openLocationDialog(() => window.location.reload()),
    );
  }
})();

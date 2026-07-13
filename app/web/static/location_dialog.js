// Shared "New location" dialog (spec §7). Loaded on every authenticated page;
// active only where the _location_dialog.html partial is present. Exposes
// `openLocationDialog(onCreated)` — `onCreated` receives the created location so
// each caller can react (reload the tree, select it in a picker…).
// Uses shared.js helpers (csrfToken, errorMessage).

(function () {
  const dialog = document.getElementById("location-dialog");
  if (!dialog) return; // page does not include the dialog

  const form = document.getElementById("location-form");
  const errorEl = document.getElementById("location-error");
  let onCreated = null;
  // Ignore re-entrant submits while a create is in flight, so a fast double-click
  // can't POST twice and leave a duplicate Location row behind.
  let submitting = false;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    try {
      const field = (name) => form.querySelector(`[name="${name}"]`);
      const parent = field("parent_id").value;
      const body = JSON.stringify({
        type: field("type").value,
        name: field("name").value.trim(),
        parent_id: parent ? Number(parent) : null,
      });

      errorEl.hidden = true;
      let created;
      try {
        const resp = await fetch("/api/locations", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body,
        });
        if (!resp.ok) {
          errorEl.textContent = await errorMessage(resp);
          errorEl.hidden = false;
          return;
        }
        created = await resp.json();
      } catch {
        errorEl.textContent = "Could not reach the server. Please try again.";
        errorEl.hidden = false;
        return;
      }
      dialog.close();
      if (onCreated) onCreated(created);
    } finally {
      submitting = false;
    }
  });

  // Open the dialog; `callback(created)` runs after a successful create.
  window.openLocationDialog = function (callback) {
    onCreated = callback || null;
    form.reset();
    errorEl.hidden = true;
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

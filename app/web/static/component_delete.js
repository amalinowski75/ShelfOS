// Admin-only component delete (§20). The "Delete" button on the detail page opens
// a dialog listing what the delete destroys and what it leaves behind (built
// server-side), and confirming sends DELETE /api/admin/components/{id}.
//
// There is no soft delete and no undo, so the page navigates away rather than
// reloading into a 404: the component it was showing no longer exists. The toast
// is queued for the page that lands.
// csrfToken/errorMessage/showToastAfterReload come from shared.js.

(function () {
  const dialog = document.getElementById("component-delete-dialog");
  const openBtn = document.getElementById("component-delete-btn");
  if (!dialog || !openBtn) return; // non-admin, or not the detail page

  const confirmBtn = document.getElementById("component-delete-confirm");
  const error = document.getElementById("component-delete-error");
  const componentId = dialog.dataset.componentId;
  // Named by the server: the dialog already says it, and the toast on the page
  // that lands should call the component the same thing this one did.
  const name = dialog.dataset.name || `component #${componentId}`;

  openBtn.addEventListener("click", () => {
    error.hidden = true;
    dialog.showModal();
  });

  // Stays true after a successful delete: the navigation is PENDING while the
  // browser fetches the next page, so the dialog is still on screen and still
  // clickable, and a second DELETE would answer 404 and show that as a failure
  // of a delete that in fact worked.
  let deleting = false;
  confirmBtn.addEventListener("click", async () => {
    if (deleting) return;
    deleting = true;
    error.hidden = true;
    let resp;
    try {
      resp = await fetch(`/api/admin/components/${componentId}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken },
      });
    } catch {
      error.textContent = "Could not reach the server. Please try again.";
      error.hidden = false;
      deleting = false;
      return;
    }
    if (!resp.ok) {
      error.textContent = await errorMessage(resp);
      error.hidden = false;
      deleting = false;
      return;
    }
    showToastAfterReload(`Deleted ${name}.`, { tone: "ok" });
    window.location.assign("/");
  });
})();

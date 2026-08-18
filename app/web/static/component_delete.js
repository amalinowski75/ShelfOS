// Admin-only component delete and restore (§20). The delete is SOFT: the row
// stays and the component stops being usable, so the page it happened on is
// still there afterwards — this reloads rather than navigating away, and the
// reloaded page carries the banner and the way back.
//
// Why soft: a hard delete leaves the invoice lines and stock movements that name
// the component pointing at an id SQLite hands to the next component created, so
// the replacement part inherits the dead one's purchase history.
// csrfToken/errorMessage/showToastAfterReload come from shared.js.

(function () {
  const dialog = document.getElementById("component-delete-dialog");
  const restoreBtn = document.getElementById("component-restore-btn");
  const openBtn = document.getElementById("component-delete-btn");

  // Stays true after a success: the reload is a PENDING navigation, so the page
  // is still on screen and still clickable while it happens, and a second write
  // would answer 422 ("already deleted") and report a delete that worked as one
  // that failed.
  let working = false;

  async function send(url, { method, onFail }) {
    if (working) return;
    working = true;
    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers: { "X-CSRF-Token": csrfToken },
      });
    } catch {
      onFail("Could not reach the server. Please try again.");
      working = false;
      return;
    }
    if (!resp.ok) {
      onFail(await errorMessage(resp));
      working = false;
      return;
    }
    return resp;
  }

  if (dialog && openBtn) {
    const confirmBtn = document.getElementById("component-delete-confirm");
    const error = document.getElementById("component-delete-error");
    const reason = document.getElementById("component-delete-reason");
    const componentId = dialog.dataset.componentId;
    // Named by the server: the toast on the reloaded page should call the
    // component what this dialog called it.
    const name = dialog.dataset.name || `component #${componentId}`;

    openBtn.addEventListener("click", () => {
      error.hidden = true;
      dialog.showModal();
    });

    // Absent when the component still holds stock: the dialog then only explains
    // why, since the click could do nothing but earn a 422.
    if (confirmBtn) {
      confirmBtn.addEventListener("click", async () => {
        error.hidden = true;
        const params = new URLSearchParams();
        if (reason && reason.value.trim())
          params.set("reason", reason.value.trim());
        const query = params.toString();
        const ok = await send(
          `/api/admin/components/${componentId}${query ? `?${query}` : ""}`,
          {
            method: "DELETE",
            onFail: (message) => {
              error.textContent = message;
              error.hidden = false;
            },
          },
        );
        if (!ok) return;
        showToastAfterReload(`${name} is no longer in use.`, { tone: "ok" });
        window.location.reload();
      });
    }
  }

  if (restoreBtn) {
    restoreBtn.addEventListener("click", async () => {
      const ok = await send(
        `/api/admin/components/${restoreBtn.dataset.componentId}/restore`,
        { method: "POST", onFail: (message) => showToast(message) },
      );
      if (!ok) return;
      showToastAfterReload("Back in use.", { tone: "ok" });
      window.location.reload();
    });
  }
})();

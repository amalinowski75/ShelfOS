// Undoing a BOM take. The reason is required — a reversal that does not say what
// happened to the board is a hole in the record the snapshot exists to keep — so
// Confirm stays disabled until something is typed.

const takeUndoDialog = document.getElementById("take-undo-dialog");
if (takeUndoDialog) {
  // The id rides on the dialog, which is rendered only when there is something to
  // undo — a read-only account and an already-reversed take get neither.
  const takeId = takeUndoDialog.dataset.takeId;
  const reason = document.getElementById("take-undo-reason");
  const confirm = document.getElementById("take-undo-confirm");
  const errorRow = document.getElementById("take-undo-error-row");
  const error = document.getElementById("take-undo-error");

  const setEnabled = () => {
    confirm.disabled = !reason.value.trim();
  };
  reason.addEventListener("input", setEnabled);

  document.getElementById("take-undo")?.addEventListener("click", () => {
    reason.value = "";
    setEnabled();
    errorRow.hidden = true;
    takeUndoDialog.showModal();
  });

  for (const button of takeUndoDialog.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => takeUndoDialog.close());
  }

  let running = false;
  confirm.addEventListener("click", async () => {
    if (running || !reason.value.trim()) return;
    running = true;
    confirm.disabled = true;
    try {
      const resp = await fetch(`/api/bom-takes/${takeId}/reverse`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ reason: reason.value }),
      });
      if (resp.ok) {
        // Reload rather than patch the page: the whole thing changes — the badge,
        // the banner, and the button that is no longer offered.
        window.location.reload();
        return;
      }
      error.textContent = await errorMessage(resp);
    } catch {
      error.textContent = "Could not reach the server.";
    }
    errorRow.hidden = false;
    running = false;
    setEnabled();
  });
}

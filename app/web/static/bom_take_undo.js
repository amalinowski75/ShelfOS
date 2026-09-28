// Undoing a BOM take. The reason is required — a reversal that does not say what
// happened to the board is a hole in the record the snapshot exists to keep — so
// Confirm stays disabled until something is typed. When a bin the take drew from
// has been deleted since, the dialog also carries a location picker, and Confirm
// waits for that too: those parts have no bin of their own to go back to.

const takeUndoDialog = document.getElementById("take-undo-dialog");
if (takeUndoDialog) {
  // The id rides on the dialog, which is rendered only when there is something to
  // undo — a read-only account and an already-reversed take get neither.
  const takeId = takeUndoDialog.dataset.takeId;
  const reason = document.getElementById("take-undo-reason");
  const confirm = document.getElementById("take-undo-confirm");
  const errorRow = document.getElementById("take-undo-error-row");
  const error = document.getElementById("take-undo-error");
  const returnTo = document.getElementById("take-undo-location");

  const setEnabled = () => {
    confirm.disabled = !reason.value.trim() || (returnTo !== null && !returnTo.value);
  };
  reason.addEventListener("input", setEnabled);
  returnTo?.addEventListener("change", setEnabled);

  document.getElementById("take-undo")?.addEventListener("click", () => {
    reason.value = "";
    if (returnTo) returnTo.value = "";
    setEnabled();
    errorRow.hidden = true;
    takeUndoDialog.showModal();
  });

  for (const button of takeUndoDialog.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => takeUndoDialog.close());
  }

  let running = false;
  confirm.addEventListener("click", async () => {
    if (running || confirm.disabled) return;
    running = true;
    confirm.disabled = true;
    const body = { reason: reason.value };
    if (returnTo) body.return_location_id = Number(returnTo.value);
    try {
      const resp = await fetch(`/api/bom-takes/${takeId}/reverse`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(body),
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

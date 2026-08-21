// The BOM list (spec §21): upload a KiCad BOM CSV and jump to its report, and
// delete a BOM from its row. The list itself is server-rendered; this only wires
// the multipart upload (like the attachment upload) and the delete. `csrfToken`,
// `errorMessage` and `showToastAfterReload` come from shared.js.

const uploadBtn = document.getElementById("bom-upload-btn");
if (uploadBtn) {
  const dialog = document.getElementById("bom-upload-dialog");
  const form = document.getElementById("bom-upload-form");
  const error = document.getElementById("bom-upload-error");

  uploadBtn.addEventListener("click", () => {
    form.reset();
    error.hidden = true;
    dialog.showModal();
  });

  let submitting = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    (async () => {
      try {
        // FormData drives the multipart request; no Content-Type header so the
        // browser sets the boundary.
        const resp = await fetch("/api/boms", {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken },
          body: new FormData(form),
        });
        if (resp.ok) {
          const bom = await resp.json();
          window.location = `/boms/${bom.id}`; // straight to the report
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      } finally {
        submitting = false;
      }
    })();
  });
}

// Delete a BOM from its row. The rows are server-rendered, so the page reloads to
// reflect the change; the confirmation names the BOM, because a delete also takes
// the stored CSV with it and there is no undo.
let deleting = false;
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-bom-delete]");
  if (!button || deleting) return;
  const id = button.dataset.bomDelete;
  const name = button.dataset.bomName || "this BOM";
  if (!confirm(`Delete "${name}" and its stored CSV? This cannot be undone.`)) return;
  deleting = true;
  (async () => {
    try {
      const resp = await fetch(`/api/boms/${id}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (resp.ok) {
        showToastAfterReload(`Deleted "${name}".`, { tone: "ok" });
        window.location.reload();
      } else {
        alert(await errorMessage(resp));
      }
    } catch {
      alert("Could not reach the server.");
    } finally {
      deleting = false;
    }
  })();
});

// BOM import (spec §21): upload a KiCad BOM CSV and jump to its report. The
// report itself is server-rendered; this only wires the multipart upload (like
// the attachment upload). `csrfToken` and `errorMessage` come from shared.js.

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

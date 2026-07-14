// "Change password" dialog, available to every signed-in account from the top
// bar (spec §18). Self-service: it verifies the current password server-side and
// sets the new one via /api/auth/change-password (not the admin API), so any
// role — including read-only — can rotate its own credentials. `csrfToken` and
// `errorMessage` come from shared.js; [data-close] buttons are wired there.

const changePasswordBtn = document.getElementById("change-password-btn");
if (changePasswordBtn) {
  const dialog = document.getElementById("password-dialog");
  const form = document.getElementById("password-form");
  const error = document.getElementById("password-error");

  changePasswordBtn.addEventListener("click", () => {
    form.reset();
    error.hidden = true;
    dialog.showModal();
  });

  // Ignore a re-entrant submit while a change is in flight (stops a double-click
  // sending two requests).
  let submitting = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    (async () => {
      try {
        const resp = await fetch("/api/auth/change-password", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            current_password: form.current_password.value,
            new_password: form.new_password.value,
          }),
        });
        if (resp.ok) {
          dialog.close();
          alert("Password changed.");
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

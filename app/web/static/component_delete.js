// Admin-only component delete and restore (§20). The delete is SOFT: the row
// stays and the component stops being usable, so the page it happened on is
// still there afterwards — this reloads rather than navigating away, and the
// reloaded page carries the banner and the way back.
//
// Why soft: a hard delete leaves the invoice lines and stock movements that name
// the component pointing at an id SQLite hands to the next component created, so
// the replacement part inherits the dead one's purchase history.
// csrfToken/errorTextFromBody/showToastAfterReload come from shared.js.

(function () {
  const dialog = document.getElementById("component-delete-dialog");
  const restoreBtn = document.getElementById("component-restore-btn");
  const openBtn = document.getElementById("component-delete-btn");

  // Shared by both buttons; only one of them is ever on the page. It is released
  // again after a FAILURE, so the admin can retry — but never after a success,
  // because the reload that follows is a pending navigation: the page stays on
  // screen and clickable while it happens, and a second write would answer 422
  // ("already deleted") and report a write that worked as one that failed.
  let working = false;

  async function send(url, { method, body, onFail }) {
    if (working) return;
    working = true;
    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers: {
          "X-CSRF-Token": csrfToken,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
    } catch {
      onFail(null, "Could not reach the server. Please try again.");
      working = false;
      return;
    }
    if (!resp.ok) {
      let parsed = null;
      try {
        parsed = await resp.json();
      } catch {
        /* a proxy's HTML, or no body at all */
      }
      onFail(parsed, errorTextFromBody(parsed, "Request failed"));
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

    // Absent when something still points at the component: the dialog then only
    // explains what, since the click could do nothing but earn a 422.
    if (confirmBtn) {
      confirmBtn.addEventListener("click", async () => {
        error.hidden = true;
        // In the body, not the query string — see ComponentDelete's docstring.
        const typed = reason ? reason.value.trim() : "";
        const ok = await send(`/api/admin/components/${componentId}`, {
          method: "DELETE",
          body: { reason: typed || null },
          onFail: (_body, message) => {
            error.textContent = message;
            error.hidden = false;
          },
        });
        if (!ok) return;
        showToastAfterReload(`${name} is no longer in use.`, { tone: "ok" });
        window.location.reload();
      });
    }
  }

  if (restoreBtn) {
    const error = document.getElementById("component-restore-error");

    // The usual refusal is "a replacement has taken this MPN", which asks the
    // admin to go and look at a different component — so it is shown inline
    // rather than in a toast that takes the answer away after five seconds, and
    // the id the API sends alongside it becomes a link instead of being dropped.
    // DOM nodes, not innerHTML, so server text cannot inject markup.
    function showRestoreError(body, message) {
      error.replaceChildren();
      // Number() guards the href: existing_id is always a server-issued int, but
      // never build a path from an unchecked value.
      const existingId = body && Number(body.existing_id);
      if (existingId) {
        error.append(document.createTextNode(message + " "));
        const link = document.createElement("a");
        link.href = `/components/${existingId}`;
        link.textContent = "View the existing component";
        error.append(link);
      } else {
        error.textContent = message;
      }
      error.hidden = false;
    }

    restoreBtn.addEventListener("click", async () => {
      error.hidden = true;
      const ok = await send(
        `/api/admin/components/${restoreBtn.dataset.componentId}/restore`,
        { method: "POST", onFail: showRestoreError },
      );
      if (!ok) return;
      showToastAfterReload("Back in use.", { tone: "ok" });
      window.location.reload();
    });
  }
})();

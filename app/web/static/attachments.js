// Attachments panel (spec §10): lists an entity's files with download links,
// lets a writer upload (multipart) and delete. Enhances every
// `.attachments-widget` on the page, reading its entity from data attributes.
// `esc`, `csrfToken`, `errorMessage` and `canWrite` come from shared.js.

function setupAttachments(widget) {
  const entityType = widget.dataset.entityType;
  const entityId = widget.dataset.entityId;
  const list = widget.querySelector(".attachment-list");
  const empty = widget.querySelector(".attachment-empty");
  const form = widget.querySelector(".attachment-form");
  const feed =
    `/api/attachments?entity_type=${encodeURIComponent(entityType)}` +
    `&entity_id=${encodeURIComponent(entityId)}`;
  const emptyText = empty.textContent; // the macro's "No attachments." default

  async function load() {
    try {
      const resp = await fetch(feed);
      if (!resp.ok) throw new Error("failed to load attachments");
      render(await resp.json());
    } catch {
      // Show the failure instead of leaving the panel silently blank.
      list.replaceChildren();
      empty.textContent = "Could not load attachments — refresh to try again.";
      empty.hidden = false;
    }
  }

  function render(rows) {
    list.replaceChildren();
    empty.textContent = emptyText;
    empty.hidden = rows.length > 0;
    for (const row of rows) {
      list.appendChild(rowItem(row));
    }
  }

  function rowItem(row) {
    const li = document.createElement("li");
    li.className = "attachment-item";
    const notes = row.notes
      ? `<span class="muted attachment-notes">${esc(row.notes)}</span>`
      : "";
    li.innerHTML =
      `<a class="cell-mono" href="/api/attachments/${row.id}/download">${esc(row.filename)}</a>` +
      `<span class="badge b-neutral">${esc(row.kind)}</span>${notes}`;
    if (canWrite) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-ghost btn-sm";
      del.textContent = "Delete";
      del.addEventListener("click", () => remove(row));
      li.appendChild(del);
    }
    return li;
  }

  let deleting = false;
  async function remove(row) {
    if (deleting || !confirm(`Delete ${row.filename}?`)) return;
    deleting = true;
    try {
      const resp = await fetch(`/api/attachments/${row.id}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (resp.ok) {
        await load();
      } else {
        alert(await errorMessage(resp));
      }
    } catch {
      alert("Could not reach the server.");
    } finally {
      deleting = false;
    }
  }

  if (form) {
    const error = form.querySelector(".attachment-error");
    let submitting = false;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (submitting) return;
      submitting = true;
      (async () => {
        try {
          // FormData drives the multipart request; do NOT set Content-Type —
          // the browser adds it with the correct boundary.
          const body = new FormData(form);
          body.append("entity_type", entityType);
          body.append("entity_id", entityId);
          const resp = await fetch("/api/attachments", {
            method: "POST",
            headers: { "X-CSRF-Token": csrfToken },
            body,
          });
          if (resp.ok) {
            form.reset();
            error.hidden = true;
            await load();
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

  load();
}

document.querySelectorAll(".attachments-widget").forEach(setupAttachments);

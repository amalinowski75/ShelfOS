// Attachments panel (spec §10): lists an entity's files with download links,
// lets a writer upload (multipart), fetch from a URL, and delete. Enhances every
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

  async function load(fresh = false) {
    try {
      // Shared with the image gallery so the page fetches the list once; after
      // our own writes, pass fresh to bypass that cache.
      render(await fetchAttachmentList(feed, { fresh }));
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
        await load(true);
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
            await load(true);
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

  // Second writer path: attach a file the server fetches from a URL (JSON, not
  // multipart). The fetch is SSRF-guarded server-side.
  const urlForm = widget.querySelector(".attachment-url-form");
  if (urlForm) {
    const urlError = urlForm.querySelector(".attachment-url-error");
    let fetching = false;
    urlForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (fetching) return;
      fetching = true;
      (async () => {
        try {
          const resp = await fetch("/api/attachments/from-url", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
            body: JSON.stringify({
              entity_type: entityType,
              entity_id: Number(entityId),
              url: urlForm.elements.url.value.trim(),
              kind: urlForm.elements.kind.value,
              notes: urlForm.elements.notes.value.trim() || null,
            }),
          });
          if (resp.ok) {
            urlForm.reset();
            urlError.hidden = true;
            await load(true);
          } else {
            urlError.textContent = await errorMessage(resp);
            urlError.hidden = false;
          }
        } catch {
          urlError.textContent = "Could not reach the server.";
          urlError.hidden = false;
        } finally {
          fetching = false;
        }
      })();
    });
  }

  load();
}

document.querySelectorAll(".attachments-widget").forEach(setupAttachments);

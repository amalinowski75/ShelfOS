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
  const dialog = widget.querySelector(".attachment-dialog");
  const addBtn = widget.querySelector(".attachment-add");
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

  // One form, two sources: a picked file (multipart) or a URL the server fetches
  // (JSON, SSRF-guarded). The submit picks whichever field is filled; if both are,
  // it asks which to use.
  if (form) {
    const error = form.querySelector(".attachment-error");
    let submitting = false;

    // The form lives in a dialog opened from the card header, so the panel stays a
    // clean list. Open with a fresh form; the shared [data-close] handler closes it.
    if (addBtn && dialog) {
      addBtn.addEventListener("click", () => {
        form.reset();
        error.hidden = true;
        dialog.showModal();
      });
    }

    function uploadFile() {
      // FormData drives the multipart request; do NOT set Content-Type — the
      // browser adds it with the correct boundary. (An empty url field rides
      // along harmlessly; the route ignores it.)
      const body = new FormData(form);
      body.append("entity_type", entityType);
      body.append("entity_id", entityId);
      return fetch("/api/attachments", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
        body,
      });
    }

    function fetchFromUrl(url) {
      return fetch("/api/attachments/from-url", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          entity_type: entityType,
          entity_id: Number(entityId),
          url,
          kind: form.elements.kind.value,
          notes: form.elements.notes.value.trim() || null,
        }),
      });
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (submitting) return;
      const hasFile = form.elements.file.files.length > 0;
      const url = form.elements.url.value.trim();
      let useUrl;
      if (hasFile && url) {
        // OK = the file, Cancel = the URL.
        useUrl = !confirm(
          "Both a file and a URL are set — use the file? (Cancel to use the URL instead.)",
        );
      } else if (url) {
        useUrl = true;
      } else if (hasFile) {
        useUrl = false;
      } else {
        error.textContent = "Choose a file or paste a URL.";
        error.hidden = false;
        return;
      }
      submitting = true;
      (async () => {
        try {
          const resp = useUrl ? await fetchFromUrl(url) : await uploadFile();
          if (resp.ok) {
            form.reset();
            error.hidden = true;
            dialog?.close();
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

  load();
}

document.querySelectorAll(".attachments-widget").forEach(setupAttachments);

// External-links panel: lists an entity's categorized URLs as clickable anchors,
// lets a writer add and delete them. Enhances every `.links-widget` on the page,
// reading its entity from data attributes. `esc`, `csrfToken`, `errorMessage` and
// `canWrite` come from shared.js.
//
// Unlike attachments a link is a URL the browser opens, not a stored file — so it
// renders as an external <a target="_blank">, never a download.

// The server already rejects any non-http(s) scheme at creation. This is the second
// layer: never build an href from a value that isn't plainly http(s), so a bad row
// (however it got stored) can't become a live javascript:/data: link.
function safeHref(url) {
  return /^https?:\/\//i.test(String(url ?? "")) ? url : null;
}

// A short display text when the user gave no label: the URL's host, else the URL.
function hostOf(url) {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

function setupLinks(widget) {
  const entityType = widget.dataset.entityType;
  const entityId = widget.dataset.entityId;
  const list = widget.querySelector(".link-list");
  const empty = widget.querySelector(".link-empty");
  const form = widget.querySelector(".link-form");
  const feed =
    `/api/links?entity_type=${encodeURIComponent(entityType)}` +
    `&entity_id=${encodeURIComponent(entityId)}`;
  const emptyText = empty.textContent; // the macro's "No links." default

  async function load() {
    try {
      const resp = await fetch(feed);
      if (!resp.ok) throw new Error("links feed failed");
      render(await resp.json());
    } catch {
      list.replaceChildren();
      empty.textContent = "Could not load links — refresh to try again.";
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
    li.className = "link-item";
    const text = esc(row.label || hostOf(row.url));
    const href = safeHref(row.url);
    // A row that isn't http(s) is shown as inert text, never a live link.
    const anchor = href
      ? `<a class="cell-mono" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${text}</a>`
      : `<span class="cell-mono">${text}</span>`;
    const notes = row.notes
      ? `<span class="muted link-notes">${esc(row.notes)}</span>`
      : "";
    li.innerHTML =
      anchor + `<span class="badge b-neutral">${esc(row.kind)}</span>${notes}`;
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
    const what = row.label || row.url;
    if (deleting || !confirm(`Delete ${what}?`)) return;
    deleting = true;
    try {
      const resp = await fetch(`/api/links/${row.id}`, {
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
    const error = form.querySelector(".link-error");
    let submitting = false;

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (submitting) return;
      const url = form.elements.url.value.trim();
      if (!url) {
        error.textContent = "Paste a URL.";
        error.hidden = false;
        return;
      }
      submitting = true;
      (async () => {
        try {
          const resp = await fetch("/api/links", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": csrfToken,
            },
            body: JSON.stringify({
              entity_type: entityType,
              entity_id: Number(entityId),
              url,
              kind: form.elements.kind.value,
              label: form.elements.label.value.trim() || null,
              notes: form.elements.notes.value.trim() || null,
            }),
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

document.querySelectorAll(".links-widget").forEach(setupLinks);

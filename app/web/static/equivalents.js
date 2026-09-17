// "Equivalent parts" panel on a component page: the other catalogue entries that
// are the SAME physical part under a different index, and the stock a BOM line
// pointed at any of them sees.
//
// Reads /api/components/<id>/equivalents, which always returns this component as a
// member, so a part on its own is a group of one and needs no special case here
// beyond choosing the empty text. `esc`, `csrfToken` and `errorMessage` come from
// shared.js.
//
// Whether the panel may be CHANGED is read from the widget, not from shared.js's
// `canWrite`. That one is role alone, while the page hides its write controls for
// a retired part too — and a Remove button surviving on a page the rest of the app
// treats as read-only would dissolve a group from the one place that still offered
// it.

(function () {
  const widget = document.querySelector(".equivalents-widget");
  if (!widget) return;

  const componentId = Number(widget.dataset.componentId);
  const canEdit = Boolean(widget.dataset.canWrite);
  const noteRow = widget.querySelector(".eq-note-row");
  const noteEl = widget.querySelector(".eq-note");
  const noteEditBtn = widget.querySelector(".eq-note-edit");
  const noteForm = widget.querySelector(".eq-note-form");
  const noteInput = widget.querySelector(".eq-note-input");
  const noteError = widget.querySelector(".eq-note-error");
  const tableWrap = widget.querySelector(".eq-table-wrap");
  const rowsEl = widget.querySelector(".eq-rows");
  const footEl = widget.querySelector(".eq-foot");
  const emptyEl = widget.querySelector(".eq-empty");
  const addBtn = widget.querySelector(".eq-add");
  const dialog = widget.querySelector(".eq-dialog");
  const emptyText = emptyEl.textContent;

  function cellLink(row) {
    const label = esc(row.mpn || `#${row.component_id}`);
    // The row for the component whose page this is: its own name, not a link back
    // to where you already are.
    return row.component_id === componentId
      ? `<span class="cell-mono">${label}</span>`
      : `<a class="cell-mono" href="/components/${Number(row.component_id)}">${label}</a>`;
  }

  // The group as last read, so the note editor and the add dialog know whether
  // there is a group to write a note to.
  let current = null;

  function renderNote(group) {
    const grouped = group.group_id != null;
    noteEl.textContent = group.notes || "";
    noteEl.hidden = !group.notes;
    if (noteEditBtn) {
      // "Add a note" rather than "Edit note" when there is nothing to edit: a
      // group created without one must still be explainable afterwards.
      noteEditBtn.textContent = group.notes ? "Edit note" : "Add a note";
      noteEditBtn.hidden = !grouped;
    }
    if (noteForm) noteForm.hidden = true;
    if (noteError) noteError.hidden = true;
    // Nothing to show and nothing to press: an ungrouped part has no note, and a
    // reader of a group without one has no reason to see an empty row.
    noteRow.hidden = !grouped || (!group.notes && !canEdit);
  }

  function render(group) {
    current = group;
    const members = group.members || [];
    rowsEl.replaceChildren();
    footEl.replaceChildren();
    renderNote(group);
    // One member is this component alone — a group of one says nothing, and the
    // server deletes such a group, so it is the "not grouped" state.
    const grouped = members.length > 1;
    tableWrap.hidden = !grouped;
    emptyEl.textContent = emptyText;
    emptyEl.hidden = grouped;
    if (!grouped) return;

    for (const row of members) {
      const tr = document.createElement("tr");
      const retired = row.deleted
        ? ' <span class="badge b-warn"><span class="dot"></span>out of use</span>'
        : "";
      tr.innerHTML =
        `<td>${cellLink(row)}${retired}</td>` +
        `<td>${esc(row.manufacturer || "—")}</td>` +
        `<td>${esc(row.package || "—")}</td>` +
        `<td class="num">${Number(row.stock)}</td>`;
      const actions = document.createElement("td");
      actions.style.textAlign = "right";
      if (canEdit) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-ghost btn-sm";
        remove.textContent = "Remove";
        remove.addEventListener("click", () => unlink(row));
        actions.appendChild(remove);
      }
      tr.appendChild(actions);
      rowsEl.appendChild(tr);
    }
    // The number the BOM will use, spelled out under the column it comes from.
    // It is the plain sum of the column: a member taken out of use adds nothing
    // to it, because a part cannot be retired while its stock is on the shelf.
    const foot = document.createElement("tr");
    foot.innerHTML =
      `<td colspan="3"><strong>Total a BOM line sees</strong></td>` +
      `<td class="num"><strong>${Number(group.total_stock)}</strong></td><td></td>`;
    footEl.appendChild(foot);
  }

  async function load() {
    try {
      const resp = await fetch(`/api/components/${componentId}/equivalents`);
      if (!resp.ok) throw new Error("equivalents feed failed");
      render(await resp.json());
    } catch {
      rowsEl.replaceChildren();
      footEl.replaceChildren();
      tableWrap.hidden = true;
      noteRow.hidden = true;
      current = null;
      emptyEl.textContent = "Could not load equivalent parts — refresh to try again.";
      emptyEl.hidden = false;
    }
  }

  let removing = false;
  async function unlink(row) {
    const what = row.mpn || `#${row.component_id}`;
    // Says what it costs: dropping the last partner dissolves the group, so this
    // is not always "one row goes away".
    if (removing || !confirm(`Stop treating ${what} as the same part?`)) return;
    removing = true;
    try {
      const resp = await fetch(
        `/api/components/${componentId}/equivalents/${Number(row.component_id)}`,
        { method: "DELETE", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!resp.ok) {
        alert(await errorMessage(resp));
        return;
      }
      // Removing THIS component leaves the page looking at a part that is no
      // longer in the group it is showing; reload so what is on screen is true.
      await load();
    } catch {
      alert("Could not reach the server.");
    } finally {
      removing = false;
    }
  }

  // --- the note: why these entries are one part -----------------------------
  // The add dialog asks for it only when the group is being created, and the
  // service fills a BLANK note alone, so without this the first wording typed
  // would be the last one possible — a typo in it could not be fixed from the
  // product at all.
  if (noteForm && noteEditBtn) {
    let saving = false;

    noteEditBtn.addEventListener("click", () => {
      noteInput.value = (current && current.notes) || "";
      noteError.hidden = true;
      noteForm.hidden = false;
      noteRow.hidden = true;
      noteInput.focus();
    });

    noteForm.querySelector(".eq-note-cancel").addEventListener("click", () => {
      if (current) renderNote(current);
    });

    noteForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (saving) return;
      saving = true;
      // Sent as typed, blank included: clearing a note that turned out to be
      // wrong is as much an edit as rewriting it, and the server reads an empty
      // string as "no note".
      const notes = noteInput.value.trim();
      (async () => {
        try {
          const resp = await fetch(
            `/api/components/${componentId}/equivalents/notes`,
            {
              method: "PUT",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken,
              },
              body: JSON.stringify({ notes }),
            },
          );
          if (!resp.ok) {
            noteError.textContent = await errorMessage(resp);
            noteError.hidden = false;
            return;
          }
          render(await resp.json());
        } catch {
          noteError.textContent = "Could not reach the server.";
          noteError.hidden = false;
        } finally {
          saving = false;
        }
      })();
    });
  }

  if (!dialog || !addBtn) {
    load();
    return;
  }

  const searchEl = dialog.querySelector(".eq-search");
  const notesEl = dialog.querySelector(".eq-notes");
  const notesField = dialog.querySelector(".eq-notes-field");
  const errorEl = dialog.querySelector(".eq-error");
  const resultsEl = dialog.querySelector(".eq-results");
  const resultsWrap = dialog.querySelector(".eq-results-wrap");
  const resultsEmpty = dialog.querySelector(".eq-results-empty");

  function setError(text) {
    errorEl.textContent = text || "";
    errorEl.hidden = !text;
  }

  function showResults(rows) {
    resultsEl.replaceChildren();
    resultsWrap.hidden = rows.length === 0;
    resultsEmpty.hidden = rows.length > 0;
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="cell-mono">${esc(row.mpn || `#${row.component_id}`)}</td>` +
        `<td>${esc(row.manufacturer || "—")}</td>` +
        `<td>${esc(row.package || "—")}</td>` +
        `<td class="num">${Number(row.stock)}</td>`;
      const actions = document.createElement("td");
      actions.style.textAlign = "right";
      if (row.grouped) {
        // Listed but not offered: the server would refuse this, and saying so
        // here beats a failed click that looks like a bug.
        actions.innerHTML = '<span class="muted">in another group</span>';
      } else {
        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn-secondary btn-sm";
        add.textContent = "Same part";
        add.addEventListener("click", () => link(row));
        actions.appendChild(add);
      }
      tr.appendChild(actions);
      resultsEl.appendChild(tr);
    }
  }

  // Bumped by every search and every close, so a slow response cannot paint its
  // rows over a newer query's — or over a reopened dialog.
  let searchToken = 0;
  async function search() {
    const query = searchEl.value.trim();
    const token = ++searchToken;
    if (!query) {
      showResults([]);
      resultsEmpty.textContent = "Type part of an MPN to search.";
      return;
    }
    try {
      const resp = await fetch(
        `/api/components/${componentId}/equivalents/candidates` +
          `?q=${encodeURIComponent(query)}`,
      );
      if (!resp.ok) throw new Error("candidate search failed");
      const rows = await resp.json();
      if (token !== searchToken) return;
      resultsEmpty.textContent = "Nothing matches that.";
      showResults(rows);
    } catch {
      if (token !== searchToken) return;
      showResults([]);
      resultsEmpty.textContent = "Could not search the catalogue.";
    }
  }

  let linking = false;
  async function link(row) {
    if (linking) return;
    linking = true;
    setError("");
    try {
      const resp = await fetch(`/api/components/${componentId}/equivalents`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          component_id: Number(row.component_id),
          notes: notesEl.value.trim() || null,
        }),
      });
      if (!resp.ok) {
        setError(await errorMessage(resp));
        return;
      }
      render(await resp.json());
      // The dialog stays open: a part with three variants is added three times,
      // and closing after each one would mean reopening and retyping the search.
      // Re-run it so the part just added drops out of the list.
      await search();
    } catch {
      setError("Could not reach the server.");
    } finally {
      linking = false;
    }
  }

  let searchTimer = null;
  searchEl.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, 200);
  });
  searchEl.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault(); // inside a <dialog>, Enter would submit and close it
    clearTimeout(searchTimer);
    search();
  });

  dialog.addEventListener("close", () => {
    searchToken += 1;
    setError("");
  });

  addBtn.addEventListener("click", () => {
    setError("");
    notesEl.value = "";
    // Asked only while the group is being created. Once it exists the note is
    // edited on the panel, and an input here would be one the server ignores —
    // `link_components` fills a blank note, it does not overwrite one.
    notesField.hidden = Boolean(current && current.group_id != null);
    // Start from this part's own number: the variants of "AO3400A" are spelled
    // "AO3400A-TR" and "AO3400A/BULK", so its MPN is the search that finds them.
    searchEl.value = widget.dataset.mpn || "";
    showResults([]);
    resultsEmpty.textContent = "Type part of an MPN to search.";
    dialog.showModal();
    searchEl.focus();
    search();
  });

  load();
})();

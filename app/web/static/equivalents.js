// "Equivalent parts" panel on a component page: the other catalogue entries that
// are the SAME physical part under a different index, and the stock a BOM line
// pointed at any of them sees.
//
// Reads /api/components/<id>/equivalents, which always returns this component as a
// member, so a part on its own is a group of one and needs no special case here
// beyond choosing the empty text. `esc`, `csrfToken`, `errorMessage` and `canWrite`
// come from shared.js.

(function () {
  const widget = document.querySelector(".equivalents-widget");
  if (!widget) return;

  const componentId = Number(widget.dataset.componentId);
  const noteEl = widget.querySelector(".eq-note");
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

  function render(group) {
    const members = group.members || [];
    rowsEl.replaceChildren();
    footEl.replaceChildren();
    noteEl.textContent = group.notes || "";
    noteEl.hidden = !group.notes;
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
      if (canWrite) {
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
    // The number the BOM will use, spelled out under the column it comes from —
    // a total that only counts the parts still in use, which is why it can be
    // smaller than the column above it adds up to.
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
      noteEl.hidden = true;
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
    notesEl.value = noteEl.textContent || "";
    // The note belongs to the group and is only taken when it has none, so hide
    // the field once one is written rather than showing an input that is ignored.
    notesField.hidden = Boolean(noteEl.textContent);
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

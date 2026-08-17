// Type & parameter management (admin only, §13 edit). A Tabulator over the
// /web/api/types feed: rename a type, delete an unused one, and manage its
// parameter definitions (add / edit / delete, including enum tokens). Every write
// goes through the admin API (/api/admin/…) or the type API (/api/types/…), each
// requiring an admin session + CSRF. `csrfToken`, `esc`, `errorMessage`,
// `frameTable` come from shared.js.

let typesData = []; // the last feed, so an open params dialog can re-render on reload
let openParamsTypeId = null; // the type whose parameters dialog is showing, if any

const typesTable = new Tabulator("#types-table", {
  layout: "fitDataFill",
  placeholder: "No types",
  columns: typeColumns(),
});

async function sendWrite(url, method, payload) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
  });
}

function typeColumns() {
  return [
    {
      title: "Name",
      field: "name",
      // Live substring filter on the type name (case-insensitive "like", as on the
      // components table), so a long taxonomy is quick to search.
      headerFilter: "input",
      headerFilterPlaceholder: "Filter name…",
      headerFilterParams: { elementAttributes: { "aria-label": "Filter name" } },
      formatter: (cell) => `<span class="cell-mono">${esc(cell.getValue())}</span>`,
    },
    {
      title: "Parent",
      field: "parent_name",
      formatter: (cell) =>
        cell.getValue() ? esc(cell.getValue()) : `<span class="muted">—</span>`,
    },
    { title: "Components", field: "component_count", hozAlign: "right" },
    { title: "Child types", field: "child_count", hozAlign: "right" },
    {
      title: "Parameters",
      field: "parameters",
      headerSort: false,
      formatter: (cell) => String((cell.getValue() || []).length),
    },
    {
      title: "",
      field: "actions",
      headerSort: false,
      width: 300,
      hozAlign: "right",
      formatter: () =>
        `<div class="row-actions">
           <button class="btn btn-secondary btn-sm" data-act="params">Parameters</button>
           <button class="btn btn-secondary btn-sm" data-act="rename">Rename</button>
           <button class="btn btn-ghost btn-sm" data-act="delete">Delete</button>
         </div>`,
      cellClick: (event, cell) => {
        const act = event.target.dataset.act;
        if (!act) return;
        const row = cell.getRow().getData();
        if (act === "rename") openRenameDialog(row);
        else if (act === "params") openParamsDialog(row);
        else if (act === "delete") deleteType(row);
      },
    },
  ];
}

async function loadTypes() {
  try {
    const resp = await fetch("/web/api/types");
    if (!resp.ok) throw new Error("feed request failed");
    const payload = await resp.json();
    typesData = payload.data;
    await typesTable.setData(typesData);
    frameTable(typesTable);
    refreshOpenParams();
  } catch {
    await typesTable.setData([]);
    frameTable(typesTable);
    alert("Could not load types — refresh to try again.");
  }
}

function makeGuard() {
  let inFlight = false;
  return async (run) => {
    if (inFlight) return;
    inFlight = true;
    try {
      await run();
    } finally {
      inFlight = false;
    }
  };
}

// Split the enum-token field (one token per line) into clean tokens. A newline
// separator — not a comma — because a comma is a legitimate character in a token
// (a decimal comma, "1,5 mm", is the natural spelling for the invoice data this app
// parses), so splitting on it would tear such tokens apart.
function enumTokens(raw) {
  return raw
    .split("\n")
    .map((t) => t.trim())
    .filter(Boolean);
}

// --- rename a type ---
function openRenameDialog(row) {
  const form = document.getElementById("type-rename-form");
  form.type_id.value = row.id;
  form.elements.name.value = row.name;
  document.getElementById("type-rename-error").hidden = true;
  document.getElementById("type-rename-dialog").showModal();
}

const guardRename = makeGuard();
document.getElementById("type-rename-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const error = document.getElementById("type-rename-error");
  guardRename(async () => {
    try {
      const resp = await sendWrite(
        `/api/admin/types/${form.type_id.value}`,
        "PATCH",
        { name: form.elements.name.value.trim() },
      );
      if (resp.ok) {
        document.getElementById("type-rename-dialog").close();
        await loadTypes();
      } else {
        error.textContent = await errorMessage(resp);
        error.hidden = false;
      }
    } catch {
      error.textContent = "Could not reach the server.";
      error.hidden = false;
    }
  });
});

// --- delete a type ---
const guardDeleteType = makeGuard();
function deleteType(row) {
  // A type in use can't be deleted — go straight to the server and show WHY (it
  // names the count: "12 components use this type"), rather than confirming a
  // deletion that won't happen. A deletable one confirms first: it's destructive.
  if (row.deletable && !confirm(`Delete type "${row.name}"? This cannot be undone.`)) {
    return;
  }
  guardDeleteType(async () => {
    try {
      const resp = await sendWrite(`/api/admin/types/${row.id}`, "DELETE");
      if (resp.ok) await loadTypes();
      // A transient bottom-of-page notice (showToast, from shared.js), not a modal
      // alert — it names why: "12 components use this type".
      else showToast(await errorMessage(resp));
    } catch {
      showToast("Could not reach the server.");
    }
  });
}

// --- a type's parameters ---
function openParamsDialog(row) {
  openParamsTypeId = row.id;
  document.getElementById("type-params-name").textContent = row.name;
  renderParams(row);
  document.getElementById("type-params-dialog").showModal();
}

// Forget which type's list is showing once the dialog closes, so a later
// loadTypes() doesn't re-render into a closed dialog.
document
  .getElementById("type-params-dialog")
  ?.addEventListener("close", () => {
    openParamsTypeId = null;
  });

// Re-render the parameters dialog from the freshest feed after a write, so an edit
// or delete is reflected without closing it. No-op when the dialog is closed.
function refreshOpenParams() {
  if (openParamsTypeId == null) return;
  const row = typesData.find((t) => t.id === openParamsTypeId);
  if (row) renderParams(row);
}

function renderParams(row) {
  const list = document.getElementById("type-params-list");
  const empty = document.getElementById("type-params-empty");
  list.replaceChildren();
  const params = row.parameters || [];
  empty.hidden = params.length > 0;
  for (const param of params) {
    const li = document.createElement("li");
    li.className = "param-list-row";
    // Two weights: the label reads first, the technical detail is muted beneath.
    const unit = param.unit ? ` (${param.unit})` : "";
    const enums =
      param.data_type === "enum" && param.enum_values.length
        ? ` · ${param.enum_values.join(", ")}`
        : "";
    // "· N in use" so the admin can see why a Delete is disabled before hovering.
    const inUse = param.in_use_count ? ` · ${param.in_use_count} in use` : "";
    const meta = document.createElement("div");
    meta.className = "param-list-meta";
    const label = document.createElement("div");
    label.className = "param-list-label";
    label.textContent = param.label; // textContent — user text, never innerHTML
    const detail = document.createElement("div");
    detail.className = "param-list-detail";
    detail.textContent = `${param.name} · ${param.data_type}${unit}${enums}${inUse}`;
    meta.append(label, detail);
    const actions = document.createElement("span");
    actions.className = "param-list-actions";
    const edit = document.createElement("button");
    edit.className = "btn btn-secondary btn-sm";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => openParamEditDialog(param));
    const del = document.createElement("button");
    del.className = "btn btn-ghost btn-sm";
    del.textContent = "Delete";
    del.addEventListener("click", () => deleteParam(param));
    actions.append(edit, del);
    li.append(meta, actions);
    list.append(li);
  }
}

// --- edit a parameter (data_type fixed) ---
function openParamEditDialog(param) {
  const form = document.getElementById("param-edit-form");
  form.reset();
  form.definition_id.value = param.id;
  form.elements.name.value = param.name;
  form.elements.label.value = param.label;
  form.elements.data_type.value = param.data_type;
  form.elements.unit.value = param.unit || "";
  form.elements.sort_order.value = param.sort_order;
  form.elements.is_table_column.checked = param.is_table_column;
  form.elements.is_filterable.checked = param.is_filterable;
  const enumWrap = form.querySelector(".param-enum");
  const isEnum = param.data_type === "enum";
  enumWrap.hidden = !isEnum;
  form.elements.enum_values.value = isEnum ? param.enum_values.join("\n") : "";
  document.getElementById("param-edit-error").hidden = true;
  document.getElementById("param-edit-dialog").showModal();
}

const guardParamEdit = makeGuard();
document.getElementById("param-edit-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const error = document.getElementById("param-edit-error");
  const isEnum = form.elements.data_type.value === "enum";
  const body = {
    name: form.elements.name.value.trim(),
    label: form.elements.label.value.trim(),
    unit: form.elements.unit.value.trim() || null,
    sort_order: Number(form.elements.sort_order.value) || 0,
    is_table_column: form.elements.is_table_column.checked,
    is_filterable: form.elements.is_filterable.checked,
  };
  if (isEnum) body.enum_values = enumTokens(form.elements.enum_values.value);
  guardParamEdit(async () => {
    try {
      const resp = await sendWrite(
        `/api/admin/parameters/${form.definition_id.value}`,
        "PATCH",
        body,
      );
      if (resp.ok) {
        document.getElementById("param-edit-dialog").close();
        await loadTypes();
      } else {
        error.textContent = await errorMessage(resp); // e.g. "3 components use value 'X7R'"
        error.hidden = false;
      }
    } catch {
      error.textContent = "Could not reach the server.";
      error.hidden = false;
    }
  });
});

// --- delete a parameter ---
const guardParamDelete = makeGuard();
function deleteParam(param) {
  // Same as deleteType: a parameter in use shows the server's reason on click
  // instead of confirming a deletion that will be refused.
  if (param.deletable && !confirm(`Delete parameter "${param.label}"?`)) return;
  guardParamDelete(async () => {
    try {
      const resp = await sendWrite(`/api/admin/parameters/${param.id}`, "DELETE");
      if (resp.ok) await loadTypes();
      else showToast(await errorMessage(resp)); // e.g. "5 components have a value…"
    } catch {
      showToast("Could not reach the server.");
    }
  });
}

// --- add a parameter to the open type ---
const paramAddForm = document.getElementById("param-add-form");
if (paramAddForm) {
  const dialog = document.getElementById("param-add-dialog");
  const enumWrap = paramAddForm.querySelector(".param-enum");
  // Show the allowed-values field only for an enum parameter.
  paramAddForm.elements.data_type.addEventListener("change", () => {
    enumWrap.hidden = paramAddForm.elements.data_type.value !== "enum";
  });
  document.getElementById("type-params-add")?.addEventListener("click", () => {
    if (openParamsTypeId == null) return;
    paramAddForm.reset();
    paramAddForm.type_id.value = openParamsTypeId;
    enumWrap.hidden = paramAddForm.elements.data_type.value !== "enum";
    document.getElementById("param-add-error").hidden = true;
    dialog.showModal();
  });

  const guardParamAdd = makeGuard();
  paramAddForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const error = document.getElementById("param-add-error");
    const isEnum = paramAddForm.elements.data_type.value === "enum";
    const body = {
      name: paramAddForm.elements.name.value.trim(),
      label: paramAddForm.elements.label.value.trim(),
      data_type: paramAddForm.elements.data_type.value,
      unit: paramAddForm.elements.unit.value.trim() || null,
      sort_order: Number(paramAddForm.elements.sort_order.value) || 0,
      is_table_column: paramAddForm.elements.is_table_column.checked,
      is_filterable: paramAddForm.elements.is_filterable.checked,
    };
    if (isEnum) body.enum_values = enumTokens(paramAddForm.elements.enum_values.value);
    guardParamAdd(async () => {
      try {
        const resp = await sendWrite(
          `/api/types/${paramAddForm.type_id.value}/parameters`,
          "POST",
          body,
        );
        if (resp.ok) {
          dialog.close();
          await loadTypes();
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      }
    });
  });
}

// --- New Type: the standalone create flow, moved here from the components page.
// The builder itself lives in type_dialog.js (shared with the invoice/component
// flows); on create we just reload the table so the new type shows up.
document.getElementById("new-type-btn")?.addEventListener("click", () => {
  window.openTypeDialog?.(() => loadTypes());
});

typesTable.on("tableBuilt", loadTypes);

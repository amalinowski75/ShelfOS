// Component table + stock dialogs (spec §11, §14-15).
// Data mutations reuse the JSON API via fetch.
// `csrfToken`, `esc` and `errorMessage` come from shared.js.

const dialog = document.getElementById("stock-dialog");
const typeFilter = document.getElementById("type-filter");

const table = new Tabulator("#components-table", {
  layout: "fitColumns",
  placeholder: "No components",
});

// Fields the presenter always emits get bespoke formatting; anything else
// (type, manufacturer, per-type parameter columns) renders as plain text.
function columnDef(column) {
  const base = { title: column.title, field: column.field };
  switch (column.field) {
    case "mpn":
      return {
        ...base,
        formatter: (cell) => `<span class="cell-mpn">${esc(cell.getValue())}</span>`,
      };
    case "package":
      return {
        ...base,
        formatter: (cell) => `<span class="cell-mono">${esc(cell.getValue())}</span>`,
      };
    case "mounting_type":
      return {
        ...base,
        formatter: (cell) => {
          const value = cell.getValue();
          const cls = value === "THT" ? "b-accent" : "b-neutral";
          return `<span class="badge ${cls}"><span class="dot"></span>${esc(value)}</span>`;
        },
      };
    case "quantity":
      return {
        ...base,
        hozAlign: "right",
        formatter: (cell) => {
          const value = Number(cell.getValue()) || 0;
          const zero = value === 0 ? " is-zero" : "";
          return `<span class="cell-qty${zero}">${value.toLocaleString()}</span>`;
        },
      };
    default:
      return base;
  }
}

function actionColumn() {
  return {
    title: "",
    field: "actions",
    headerSort: false,
    width: 200,
    hozAlign: "right",
    formatter: () =>
      `<div class="row-actions">
         <button class="btn btn-secondary btn-sm" data-act="add">Add</button>
         <button class="btn btn-secondary btn-sm" data-act="take">Take</button>
         <button class="btn btn-ghost btn-sm" data-act="details">Details</button>
       </div>`,
    cellClick: (event, cell) => {
      const act = event.target.dataset.act;
      if (!act) return;
      const row = cell.getRow().getData();
      if (act === "details") {
        window.location = `/components/${row.id}`;
      } else {
        openStockDialog(act, row);
      }
    },
  };
}

function currentTypeQuery() {
  const value = typeFilter.value;
  return value ? `?type_id=${value}` : "";
}

async function loadTable() {
  const payload = await fetch(
    `/web/api/components${currentTypeQuery()}`,
  ).then((r) => r.json());
  const columns = payload.columns.map(columnDef);
  columns.push(actionColumn());
  table.setColumns(columns);
  await table.setData(payload.data);
}

function openStockDialog(mode, row) {
  const form = document.getElementById("stock-form");
  form.component_id.value = row.id;
  form.mode.value = mode;
  document.getElementById("stock-dialog-title").textContent =
    mode === "add" ? "Add stock" : "Take from stock";
  document.getElementById("stock-error").hidden = true;
  form.quantity.value = 1;
  form.querySelector(".loc-picker")?.reset();
  dialog.showModal();
}

// [data-close] buttons are wired once in shared.js.

document.getElementById("stock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  // The tree-picker is backed by a hidden input, so enforce the required
  // location here (there's no native `required` to lean on).
  if (form.location_id.value === "") {
    const el = document.getElementById("stock-error");
    el.textContent = "Choose a location.";
    el.hidden = false;
    return;
  }
  const body = JSON.stringify({
    component_id: Number(form.component_id.value),
    location_id: Number(form.location_id.value),
    quantity: Number(form.quantity.value),
    note: form.note.value || null,
  });
  const url = form.mode.value === "add" ? "/api/stock/add" : "/api/stock/remove";
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body,
  });
  if (resp.ok) {
    dialog.close();
    await loadTable();
  } else {
    const el = document.getElementById("stock-error");
    el.textContent = await errorMessage(resp);
    el.hidden = false;
  }
});

typeFilter.addEventListener("change", loadTable);
table.on("tableBuilt", loadTable);

// ---- New Type dialog (spec §13) --------------------------------------------
// Present only for accounts allowed to write, so the controls may be absent.
const typeDialog = document.getElementById("type-dialog");
const newTypeBtn = document.getElementById("new-type-btn");

if (typeDialog && newTypeBtn) {
  const typeForm = document.getElementById("type-form");
  const paramsBox = document.getElementById("params");
  const paramsEmpty = document.getElementById("params-empty");
  const rowTemplate = document.getElementById("param-row-template");

  const refreshEmptyHint = () => {
    paramsEmpty.hidden = paramsBox.children.length > 0;
  };

  function addParamRow() {
    const row = rowTemplate.content.firstElementChild.cloneNode(true);
    const dataType = row.querySelector('[name="p-data-type"]');
    const enumField = row.querySelector(".param-enum");
    // Allowed values only make sense for an enum parameter. Sync from the
    // current value too, not just on change, so the field is correct even if
    // "enum" is ever the default-selected data type.
    const syncEnumField = () => {
      enumField.hidden = dataType.value !== "enum";
    };
    dataType.addEventListener("change", syncEnumField);
    syncEnumField();
    row.querySelector(".param-remove").addEventListener("click", () => {
      row.remove();
      refreshEmptyHint();
    });
    paramsBox.appendChild(row);
    refreshEmptyHint();
    row.querySelector('[name="p-name"]').focus();
  }

  function resetTypeForm() {
    typeForm.reset();
    paramsBox.replaceChildren();
    document.getElementById("type-error").hidden = true;
    refreshEmptyHint();
    loadInheritedParams("");
  }

  // Monotonic id so overlapping parent-select changes can't render a stale
  // response: only the newest request is allowed to touch the DOM.
  let inheritedRequestId = 0;

  // Show the effective parameter set of the chosen parent so the user can see
  // what this type will already inherit and avoid redefining it (spec §13, D3).
  async function loadInheritedParams(parentId) {
    const requestId = ++inheritedRequestId;
    const hint = document.getElementById("inherited-hint");
    const list = document.getElementById("inherited-list");
    list.replaceChildren();
    if (!parentId) {
      hint.textContent =
        "Select a parent type to see the parameters this type will inherit.";
      hint.hidden = false;
      return;
    }
    let params;
    try {
      const resp = await fetch(`/api/types/${parentId}/parameters`);
      if (!resp.ok) throw new Error();
      params = await resp.json();
    } catch {
      if (requestId !== inheritedRequestId) return; // superseded by a newer pick
      // Distinct from an empty parent: surface the failure instead of implying
      // the parent simply has no parameters.
      hint.textContent = "Could not load inherited parameters.";
      hint.hidden = false;
      return;
    }
    if (requestId !== inheritedRequestId) return; // a newer selection is in flight
    if (!params.length) {
      hint.textContent = "This parent type defines no parameters.";
      hint.hidden = false;
      return;
    }
    hint.hidden = true;
    for (const p of params) {
      const meta = [p.label, p.data_type];
      if (p.unit) meta.push(p.unit);
      let metaText = meta.map(esc).join(" · ");
      if (p.data_type === "enum" && p.enum_values?.length) {
        metaText += ` (${p.enum_values.map(esc).join(", ")})`;
      }
      const li = document.createElement("li");
      li.className = "inherited-item";
      li.innerHTML =
        `<span class="ip-name">${esc(p.name)}</span>` +
        `<span class="ip-meta">${metaText}</span>`;
      list.appendChild(li);
    }
  }

  function collectParameters() {
    return [...paramsBox.querySelectorAll(".param-row")].map((row, index) => {
      const get = (name) => row.querySelector(`[name="${name}"]`);
      const dataType = get("p-data-type").value;
      const param = {
        name: get("p-name").value.trim(),
        label: get("p-label").value.trim(),
        data_type: dataType,
        unit: get("p-unit").value.trim() || null,
        is_table_column: get("p-table").checked,
        is_filterable: get("p-filter").checked,
        sort_order: index,
      };
      // Only enum parameters carry allowed values; the API rejects them on
      // other data types, so leave the key off entirely otherwise.
      if (dataType === "enum") {
        param.enum_values = get("p-enum")
          .value.split(",")
          .map((token) => token.trim())
          .filter(Boolean);
      }
      return param;
    });
  }

  function upsertTypeOption(select, type, selected) {
    let option = [...select.options].find((o) => o.value === String(type.id));
    if (!option) {
      option = document.createElement("option");
      option.value = type.id;
      option.textContent = type.name;
      select.appendChild(option);
    }
    if (selected) select.value = String(type.id);
  }

  newTypeBtn.addEventListener("click", () => {
    resetTypeForm();
    typeDialog.showModal();
  });
  document.getElementById("add-param").addEventListener("click", addParamRow);
  typeForm
    .querySelector('[name="parent-id"]')
    .addEventListener("change", (event) =>
      loadInheritedParams(event.target.value),
    );

  typeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = JSON.stringify({
      name: typeForm.querySelector('[name="type-name"]').value.trim(),
      parent_id: typeForm.querySelector('[name="parent-id"]').value
        ? Number(typeForm.querySelector('[name="parent-id"]').value)
        : null,
      parameters: collectParameters(),
    });
    const resp = await fetch("/api/types", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body,
    });
    if (resp.ok) {
      const created = await resp.json();
      // The new type becomes the active filter (spec §13, step 4) and a valid
      // parent for the next one.
      upsertTypeOption(typeFilter, created, true);
      upsertTypeOption(typeForm.querySelector('[name="parent-id"]'), created, false);
      typeDialog.close();
      await loadTable();
    } else {
      const el = document.getElementById("type-error");
      el.textContent = await errorMessage(resp);
      el.hidden = false;
    }
  });
}

// ---- New Component trigger (spec §16.5) -----------------------------------
// The dialog lives in component_dialog.js (shared with the invoice line flow);
// here we just open it and, on success, reveal the new component in the table.
const newComponentBtn = document.getElementById("new-component-btn");
if (newComponentBtn && window.openComponentDialog) {
  newComponentBtn.addEventListener("click", () =>
    openComponentDialog((created) => {
      typeFilter.value = String(created.type_id);
      loadTable();
    }),
  );
}

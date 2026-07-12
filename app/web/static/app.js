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
  dialog.showModal();
}

// [data-close] buttons are wired once in shared.js.

document.getElementById("stock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
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

// ---- New Component dialog (spec §16.5) -------------------------------------
// Present only for accounts allowed to write, so the controls may be absent.
const componentDialog = document.getElementById("component-dialog");
const newComponentBtn = document.getElementById("new-component-btn");

if (componentDialog && newComponentBtn) {
  const componentForm = document.getElementById("component-form");
  const componentTypeSelect = document.getElementById("component-type");
  const componentParamsBox = document.getElementById("component-params");
  const componentParamsHint = document.getElementById("component-params-hint");
  const componentError = document.getElementById("component-error");

  // Monotonic id so overlapping type-select changes can't render stale fields:
  // only the newest request is allowed to touch the DOM.
  let paramsRequestId = 0;

  // Build a value input for one effective parameter definition, keyed by its id
  // and data type so the payload can be assembled without another lookup.
  function buildParamField(definition) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = definition.unit
      ? `${definition.label} (${definition.unit})`
      : definition.label;
    field.appendChild(label);

    let input;
    if (definition.data_type === "enum") {
      input = document.createElement("select");
      input.className = "control";
      input.appendChild(new Option("—", ""));
      for (const value of definition.enum_values) {
        input.appendChild(new Option(value, value));
      }
    } else if (definition.data_type === "bool") {
      input = document.createElement("select");
      input.className = "control";
      input.appendChild(new Option("—", ""));
      input.appendChild(new Option("yes", "true"));
      input.appendChild(new Option("no", "false"));
    } else {
      input = document.createElement("input");
      input.className = "control";
      input.type = "text";
      if (definition.data_type === "number") input.placeholder = "e.g. 4k7";
    }
    input.dataset.definitionId = definition.id;
    input.dataset.dataType = definition.data_type;
    field.appendChild(input);
    return field;
  }

  async function loadComponentParams(typeId) {
    const requestId = ++paramsRequestId;
    componentParamsBox.replaceChildren();
    if (!typeId) {
      componentParamsHint.textContent = "Select a type to enter its parameters.";
      componentParamsHint.hidden = false;
      return;
    }
    // Show a placeholder synchronously so a stale hint from the previous type
    // is never visible while the new type's parameters are loading.
    componentParamsHint.textContent = "Loading…";
    componentParamsHint.hidden = false;
    let definitions;
    try {
      const resp = await fetch(`/api/types/${typeId}/parameters`);
      if (!resp.ok) throw new Error();
      definitions = await resp.json();
    } catch {
      if (requestId !== paramsRequestId) return; // superseded by a newer pick
      componentParamsHint.textContent = "Could not load this type's parameters.";
      componentParamsHint.hidden = false;
      return;
    }
    if (requestId !== paramsRequestId) return; // a newer selection is in flight
    if (!definitions.length) {
      componentParamsHint.textContent = "This type has no parameters.";
      componentParamsHint.hidden = false;
      return;
    }
    componentParamsHint.hidden = true;
    for (const definition of definitions) {
      componentParamsBox.appendChild(buildParamField(definition));
    }
  }

  // Collect only the filled fields; a bool select maps to a real boolean, and a
  // number is sent as its raw string so the server parses the engineering value.
  function collectComponentParameters() {
    const params = [];
    for (const input of componentParamsBox.querySelectorAll("[data-definition-id]")) {
      const raw = input.value.trim();
      if (!raw) continue;
      const value = input.dataset.dataType === "bool" ? raw === "true" : raw;
      params.push({
        parameter_definition_id: Number(input.dataset.definitionId),
        value,
      });
    }
    return params;
  }

  newComponentBtn.addEventListener("click", () => {
    componentForm.reset();
    componentError.hidden = true;
    loadComponentParams(""); // clears the fields and shows the hint
    componentDialog.showModal();
  });

  componentTypeSelect.addEventListener("change", (event) =>
    loadComponentParams(event.target.value),
  );

  componentForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = (name) =>
      componentForm.querySelector(`[name="${name}"]`).value.trim();
    const body = JSON.stringify({
      // Guard the empty selection (Number("") is 0, not null); the server then
      // rejects a missing type cleanly rather than looking up type 0.
      type_id: componentTypeSelect.value
        ? Number(componentTypeSelect.value)
        : null,
      manufacturer: value("manufacturer") || null,
      mpn: value("mpn") || null,
      package: value("package") || null,
      mounting_type: componentForm.querySelector('[name="mounting_type"]').value,
      notes: value("notes") || null,
      parameters: collectComponentParameters(),
    });
    componentError.hidden = true;
    let created;
    try {
      const resp = await fetch("/api/components", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body,
      });
      if (!resp.ok) {
        componentError.textContent = await errorMessage(resp);
        componentError.hidden = false;
        return;
      }
      created = await resp.json();
    } catch {
      // A network failure (or an unreadable body) must surface, not vanish as an
      // unhandled rejection with the dialog silently stuck open.
      componentError.textContent = "Could not reach the server. Please try again.";
      componentError.hidden = false;
      return;
    }
    componentDialog.close();
    // Filter the table to the new component's type so it is visible.
    typeFilter.value = String(created.type_id);
    await loadTable();
  });
}

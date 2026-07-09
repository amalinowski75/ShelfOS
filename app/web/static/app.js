// Component table + stock dialogs (spec §11, §14-15).
// Data mutations reuse the JSON API via fetch.

const dialog = document.getElementById("stock-dialog");
const typeFilter = document.getElementById("type-filter");

const table = new Tabulator("#components-table", {
  layout: "fitColumns",
  placeholder: "No components",
});

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

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

document
  .querySelectorAll("[data-close]")
  .forEach((btn) => btn.addEventListener("click", () => dialog.close()));

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
    headers: { "Content-Type": "application/json" },
    body,
  });
  if (resp.ok) {
    dialog.close();
    await loadTable();
  } else {
    const error = await resp.json();
    const el = document.getElementById("stock-error");
    el.textContent = error.detail || "Request failed";
    el.hidden = false;
  }
});

typeFilter.addEventListener("change", loadTable);
table.on("tableBuilt", loadTable);

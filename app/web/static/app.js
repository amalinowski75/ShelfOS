// Component table + stock dialogs (spec §11, §14-15).
// Data mutations reuse the JSON API via fetch.

const dialog = document.getElementById("stock-dialog");
const typeFilter = document.getElementById("type-filter");

const table = new Tabulator("#components-table", {
  layout: "fitColumns",
  placeholder: "No components",
});

function actionColumn() {
  return {
    title: "",
    field: "actions",
    headerSort: false,
    width: 190,
    formatter: () =>
      `<div class="row-actions">
         <button data-act="add">Add</button>
         <button data-act="take">Take</button>
         <a data-act="details" role="button" class="secondary outline">Details</a>
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
  const columns = payload.columns.map((c) => ({ title: c.title, field: c.field }));
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

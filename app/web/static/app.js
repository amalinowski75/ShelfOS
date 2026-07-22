// Component table + stock dialogs (spec §11, §14-15).
// Data mutations reuse the JSON API via fetch.
// `csrfToken`, `esc` and `errorMessage` come from shared.js.

const dialog = document.getElementById("stock-dialog");
const typeFilter = document.getElementById("type-filter");

const table = new Tabulator("#components-table", {
  // fitDataFill: columns take their natural widths (horizontal scrollbar when they
  // overflow); when narrower than the container the row background fills the slack
  // rather than stretching a column. frameTable adds the sticky-header scroll box.
  layout: "fitDataFill",
  placeholder: "No components",
});

// Fields the presenter always emits get bespoke formatting; anything else
// (type, manufacturer, per-type parameter columns) renders as plain text.
//
// Every column also carries a live text header filter. Tabulator's default
// "input" filter uses the "like" function — a case-insensitive substring match
// applied as you type — and ANDs the active filters across columns, which is
// exactly the "simple, additive per-column filtering" we want (spec §11).
// Sort a per-type number column by the raw value the presenter sends alongside
// the engineering-formatted display string (in `<field>__n`), so e.g. 47 Ω sorts
// below 220 Ω below 1 kΩ instead of lexically. Missing values sort to one end.
function numericParamSorter(field) {
  const key = `${field}__n`;
  return (a, b, aRow, bRow) => {
    const an = aRow.getData()[key];
    const bn = bRow.getData()[key];
    if (an == null && bn == null) return 0;
    if (an == null) return -1;
    if (bn == null) return 1;
    return an - bn;
  };
}

function columnDef(column) {
  const base = {
    title: column.title,
    field: column.field,
    headerFilter: "input",
    // Name each filter after its column so the placeholder and the screen-reader
    // label distinguish otherwise-identical inputs.
    headerFilterPlaceholder: `Filter ${column.title}…`,
    headerFilterParams: {
      elementAttributes: { "aria-label": `Filter ${column.title}` },
    },
  };
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
        sorter: "number", // sort by magnitude, not lexically
        // The cell shows a thousands-separated number ("1,234") but the default
        // "like" filter only matches the raw value ("1234"); accept either so
        // typing what you see filters as expected.
        headerFilterFunc: (term, value) => {
          const needle = String(term);
          return (
            String(value).includes(needle) ||
            Number(value).toLocaleString().includes(needle)
          );
        },
        formatter: (cell) => {
          const value = Number(cell.getValue()) || 0;
          const zero = value === 0 ? " is-zero" : "";
          return `<span class="cell-qty${zero}">${value.toLocaleString()}</span>`;
        },
      };
    default:
      // Per-type number columns sort by their raw value (set by the presenter);
      // left-aligned like other param columns (the values are formatted strings).
      return column.numeric
        ? { ...base, sorter: numericParamSorter(column.field) }
        : base;
  }
}

function actionColumn() {
  // Read-only accounts can't add/take stock, so don't render those buttons for
  // them — only the read-only "Details" link. (`canWrite` from shared.js.)
  const writeButtons = canWrite
    ? `<button class="btn btn-secondary btn-sm" data-act="add">Add</button>
         <button class="btn btn-secondary btn-sm" data-act="take">Take</button>
         `
    : "";
  return {
    title: "",
    field: "actions",
    headerSort: false,
    width: canWrite ? 200 : 100,
    hozAlign: "right",
    formatter: () =>
      `<div class="row-actions">
         ${writeButtons}<button class="btn btn-ghost btn-sm" data-act="details">Details</button>
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
  frameTable(table);
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

// Ignore a re-entrant submit while that form's write is in flight, enough to
// stop a fast double-click sending a duplicate POST. Each form gets its OWN
// flag: the stock and New Type dialogs are independent, so an in-flight (or
// post-success loadTable) on one must never swallow the other's submit.
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

const guardStock = makeGuard();
document.getElementById("stock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  guardStock(async () => {
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
});

typeFilter.addEventListener("change", loadTable);
table.on("tableBuilt", loadTable);

// ---- New Type trigger (spec §13) ------------------------------------------
// The dialog itself lives in type_dialog.js (shared with the invoice line flow),
// which exposes openTypeDialog. Here we only wire the list page's "New Type" button
// and, on success, reveal the new type in the filter + table.
function upsertTypeFilterOption(type) {
  const select = document.getElementById("type-filter");
  if (!select) return;
  if (![...select.options].some((o) => o.value === String(type.id))) {
    const option = document.createElement("option");
    option.value = type.id;
    option.textContent = type.name;
    select.appendChild(option);
  }
  select.value = String(type.id);
}

const newTypeBtn = document.getElementById("new-type-btn");
if (newTypeBtn) {
  newTypeBtn.addEventListener("click", () =>
    window.openTypeDialog?.((created) => {
      upsertTypeFilterOption(created);
      return loadTable();
    }),
  );
}

// ---- New Component trigger (spec §16.5) -----------------------------------
// The dialog lives in component_dialog.js (shared with the invoice line flow);
// here we open it and, on success, go straight to the new component's detail page
// (mirrors the invoice flow). Only this standalone caller navigates — the invoice
// line and BOM "add to inventory" reuses stay put, since they create a component
// mid-task and must return to what the user was doing.
const newComponentBtn = document.getElementById("new-component-btn");
if (newComponentBtn && window.openComponentDialog) {
  newComponentBtn.addEventListener("click", () =>
    openComponentDialog((created) => {
      window.location = `/components/${created.id}`;
    }),
  );
}

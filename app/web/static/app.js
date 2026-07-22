// Component table (spec §11) and the triggers for the shared dialogs.
// Data mutations reuse the JSON API via fetch.
// `esc` comes from shared.js; the stock dialog itself lives in stock_dialog.js
// (shared with the component detail page), which exposes openStockDialog.

const typeFilter = document.getElementById("type-filter");

const table = new Tabulator("#components-table", {
  // fitDataFill: columns take their natural widths (horizontal scrollbar when they
  // overflow); when narrower than the container the row background fills the slack
  // rather than stretching a column. frameTable adds the sticky-header scroll box.
  layout: "fitDataFill",
  placeholder: "No components",
});

// ---- remembered column widths ---------------------------------------------
// Tabulator columns are drag-resizable, but loadTable rebuilds them from scratch
// on every type-filter change AND after every stock write — so without this a
// widened column snaps back within seconds of being widened. Keyed by field, so
// a per-type parameter column keeps its width too.
const COLUMN_WIDTHS_KEY = "shelfos.columnWidths";
// Widest width worth restoring. Dragging a column to 1200px on a big monitor and
// then opening the page on a laptop would otherwise recreate exactly the problem
// the compact default avoids — Qty and the row's Add/Take buttons pushed off-screen
// — on every later visit, with nothing on screen explaining why.
const MAX_REMEMBERED_WIDTH = 600;

// Read once per rebuild rather than once per column: a type-specific view has a
// dozen columns and loadTable runs on every filter change and stock write.
let columnWidths = readColumnWidths();

function readColumnWidths() {
  let stored;
  try {
    stored = JSON.parse(localStorage.getItem(COLUMN_WIDTHS_KEY));
  } catch {
    return {}; // unparseable, or storage unavailable (private mode): just don't
  }
  if (!stored || typeof stored !== "object") return {};
  // Validate on READ too, not only on write: this store is editable from devtools
  // and outlives any change to the key's shape, and a bad value here goes straight
  // to Tabulator as a column width.
  return Object.fromEntries(
    Object.entries(stored).filter(
      ([, width]) =>
        typeof width === "number" && width > 0 && width <= MAX_REMEMBERED_WIDTH,
    ),
  );
}

function rememberColumnWidth(field, width) {
  if (!field || !(width > 0)) return;
  columnWidths = {
    ...columnWidths,
    [field]: Math.min(Math.round(width), MAX_REMEMBERED_WIDTH),
  };
  try {
    localStorage.setItem(COLUMN_WIDTHS_KEY, JSON.stringify(columnWidths));
  } catch {
    // Storage full or blocked — the width just won't survive a reload.
  }
}

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
  const remembered = columnWidths[column.field];
  const base = {
    title: column.title,
    field: column.field,
    // A width the user dragged wins over any default below.
    ...(remembered ? { width: remembered } : {}),
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
    case "notes":
      return {
        ...base,
        // A STARTING width, not a maximum: fitDataFill sizes to content, so an
        // unconstrained column of free text would push Qty and the row's Add/Take
        // buttons off-screen — but a maxWidth also caps the drag handle, which
        // leaves a long description permanently unreadable in the table. This way
        // it starts compact and the user can widen it, and the width sticks.
        //
        // Hovering shows as much as the feed sent — which is trimmed, so for the
        // very longest descriptions the tooltip is no fuller than the cell. The
        // component's detail page is the one that always has it whole.
        width: base.width ?? 260,
        formatter: (cell) => {
          const value = esc(cell.getValue());
          return `<span class="cell-desc" title="${value}">${value}</span>`;
        },
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
        // This page HAS a JSON feed, so it re-pulls the table instead of reloading.
        openStockDialog(act, row.id, loadTable);
      }
    },
  };
}

function currentTypeQuery() {
  const value = typeFilter.value;
  return value ? `?type_id=${value}` : "";
}

async function loadTable() {
  columnWidths = readColumnWidths(); // another tab may have resized since
  const payload = await fetch(
    `/web/api/components${currentTypeQuery()}`,
  ).then((r) => r.json());
  const columns = payload.columns.map(columnDef);
  columns.push(actionColumn());
  table.setColumns(columns);
  await table.setData(payload.data);
  frameTable(table);
}

typeFilter.addEventListener("change", loadTable);
table.on("tableBuilt", loadTable);
table.on("columnResized", (column) =>
  rememberColumnWidth(column.getField(), column.getWidth()),
);

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

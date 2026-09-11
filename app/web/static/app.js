// Component table (spec §11) and the triggers for the shared dialogs.
// Data mutations reuse the JSON API via fetch.
// `esc` comes from shared.js; the stock dialog itself lives in stock_dialog.js
// (shared with the component detail page), which exposes openStockDialog.

const typeFilter = document.getElementById("type-filter");

const table = new Tabulator("#components-table", {
  ...TABLE_DEFAULTS,
  // fitDataFill: columns take their natural widths (horizontal scrollbar when they
  // overflow); when narrower than the container the row background fills the slack
  // rather than stretching a column. frameTable adds the sticky-header scroll box.
  layout: "fitDataFill",
  placeholder: "No components",
});

// Where a row leads. Pulled out of the handler so the destination is something a
// test can name: jsdom cannot navigate, and reports only THAT an attempt was made,
// never to where. (Same reason boms_report.js keeps bomRowTarget separate.)
function componentRowTarget(row) {
  return `/components/${row.id}`;
}

// The whole row opens the part it describes. The Details button stays: it is the
// discoverable form, and the one a keyboard can reach. But on a table this wide,
// reading a row and then crossing the screen to its far-right button was the
// common case paying for the rare one.
table.on("rowClick", (event, row) => {
  // Tabulator raises rowClick for the action cell too, so a click that landed on
  // a control belongs to that control and not to the row.
  if (event.target.closest("button, a, input, select, label")) return;
  // A drag across an MPN ends in a click on the row, and leaving the page then
  // would make the table impossible to copy out of — so the row stands down while
  // a selection is live. A selection left elsewhere does NOT wedge this, but not
  // because of the check: the browser collapses it on mousedown, long before any
  // click. The one click this really swallows is one landing INSIDE an existing
  // selection, which some browsers hold through mousedown for drag-and-drop —
  // and swallowing is the safe direction there.
  const selection = window.getSelection();
  if (selection && !selection.isCollapsed) return;
  window.location = componentRowTarget(row.getData());
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
        // Same destination as clicking the row, from the same place — the button
        // and the row must never be able to disagree about where a part lives.
        window.location = componentRowTarget(row);
      } else {
        // This page HAS a JSON feed, so it re-pulls the table instead of reloading.
        openStockDialog(act, row.id, loadTable);
      }
    },
  };
}

// ---- header stats ----------------------------------------------------------
// A summary of what the table is SHOWING, not of the whole database — which is
// why it is computed here from the rows rather than fetched from the server: the
// type filter and every per-column header filter move it for free, so "how many
// resistors are out of stock" is a filter away instead of a query away.

// Pure, so a test can hand it rows and check the arithmetic. Blank strings are
// not counted as a type or a manufacturer — the feed sends "" for both when the
// component has none, and one anonymous group is not a distinct maker.
function computeStats(rows) {
  const stats = {
    components: rows.length,
    units: 0,
    zero: 0,
    zeroShare: 0,
    types: 0,
    makers: 0,
    smt: 0,
    tht: 0,
    topQty: 0,
    topMpn: "",
  };
  const types = new Set();
  const makers = new Set();
  let top = null;
  for (const row of rows) {
    const quantity = Number(row.quantity) || 0;
    stats.units += quantity;
    if (quantity === 0) stats.zero += 1;
    if (row.type) types.add(row.type);
    if (row.manufacturer) makers.add(row.manufacturer);
    if (row.mounting_type === "SMT") stats.smt += 1;
    else if (row.mounting_type === "THT") stats.tht += 1;
    if (top === null || quantity > (Number(top.quantity) || 0)) top = row;
  }
  stats.types = types.size;
  stats.makers = makers.size;
  stats.zeroShare = rows.length ? Math.round((stats.zero / rows.length) * 100) : 0;
  if (top !== null) {
    stats.topQty = Number(top.quantity) || 0;
    // The MPN names the part, but plenty of parts have none; the type is the
    // next most recognisable thing the row carries.
    stats.topMpn = top.mpn || top.type || "";
  }
  return stats;
}

function renderStats(rows) {
  const strip = document.getElementById("component-stats");
  if (!strip) return; // no strip on this page — nothing to fill
  const stats = computeStats(rows);
  const put = (id, text) => {
    document.getElementById(id).textContent = text;
  };
  const count = (n) => n.toLocaleString();

  put("stat-components", count(stats.components));
  put("stat-units", count(stats.units));
  put("stat-types", count(stats.types));
  put("stat-makers", count(stats.makers));
  put("stat-smt", count(stats.smt));
  put("stat-tht", count(stats.tht));

  const zeroEl = document.getElementById("stat-zero");
  zeroEl.textContent = count(stats.zero);
  zeroEl.classList.toggle("is-warn", stats.zero > 0);
  put("stat-zero-share", stats.zero ? `${stats.zeroShare}% of shown` : "");

  const mounted = stats.smt + stats.tht;
  document.getElementById("stat-smt-bar").style.width = mounted
    ? `${Math.round((stats.smt / mounted) * 100)}%`
    : "0%";

  put("stat-top-qty", count(stats.topQty));
  // textContent, not innerHTML: an MPN is user text and lands here unescaped.
  put("stat-top-mpn", stats.topMpn);
}

function currentTypeQuery() {
  const value = typeFilter.value;
  return value ? `?type_id=${value}` : "";
}

async function loadTable() {
  columnWidths = readColumnWidths(); // another tab may have resized since
  // An HTTP failure body parses as JSON perfectly well and simply has no
  // `columns` key, so the map below throws on a 404 or a 500 as surely as on a
  // 200 of the wrong shape — one mechanism, caught here rather than guarded
  // against with a shape test no case could reach. Without the catch the throw
  // escaped into `tableBuilt` and the type filter's change handler, where
  // nothing was waiting for it.
  let columns = null;
  let rows = [];
  try {
    const payload = await fetch(
      `/web/api/components${currentTypeQuery()}`,
    ).then((r) => r.json());
    columns = payload.columns.map(columnDef);
    rows = payload.data;
  } catch {
    columns = null;
  }
  // A failure EMPTIES the table. From tableBuilt there is nothing to lose, but
  // from the type filter this is the whole point: the throw used to land before
  // setData, so the table kept the previous type's rows while the filter above
  // it read the new one. Rows that answer a question nobody asked are worse than
  // none, and the placeholder is what says which of the two you are looking at.
  // "refresh" here, unlike the invoice dialog's "close and try again": this table
  // IS the page, so there is no cheaper recovery to point at and nothing
  // half-typed for a reload to throw away. (Changing the type filter also retries,
  // but only by changing what you asked for, which is poor advice.)
  table.options.placeholder = columns
    ? "No components"
    : "Could not load components — refresh to try again";
  if (columns) {
    columns.push(actionColumn());
    table.setColumns(columns);
  }
  await table.setData(rows);
  // The rows that SURVIVED, not the ones that arrived: a header filter outlives
  // both setColumns and setData, and loadTable runs on every type-filter change
  // and after every Add/Take from a row button. Filling from `payload.data` here
  // would hand the strip the whole feed while the table shows a filtered slice.
  //
  // Before frameTable, which sizes the table to whatever room is left under the
  // header — so the header has to be final first.
  renderStats(table.getData("active"));
  frameTable(table);
}

// The strip's HEIGHT is fixed by its skeleton, but its WIDTH tracks the numbers
// in it — so a filter that shrinks "16,970" to "0" can pull the strip back onto
// the title's line, and clearing that filter can push it off again. frameTable
// deliberately does not re-run per keystroke, so re-fit only when the header
// really did change height; otherwise the table keeps a height measured against
// a header that is no longer there, and the page grows a scrollbar of its own.
let framedHeadHeight = null;
function refitIfHeaderResized() {
  const head = document.querySelector(".head");
  if (!head) return;
  const height = head.offsetHeight;
  if (framedHeadHeight !== null && height !== framedHeadHeight) frameTable(table);
  framedHeadHeight = height;
}

typeFilter.addEventListener("change", loadTable);
table.on("tableBuilt", loadTable);
// What makes the tiles follow the column header filters: the library re-runs
// them on every keystroke and hands us the rows that survived. It also fires
// this inside setData, before that promise resolves — which is why loadTable
// can rely on the table already knowing what is active.
table.on("dataFiltered", (filters, rows) => {
  renderStats(rows.map((row) => row.getData()));
  refitIfHeaderResized();
});
table.on("columnResized", (column) =>
  rememberColumnWidth(column.getField(), column.getWidth()),
);

// ---- New Component trigger (spec §16.5) -----------------------------------
// The dialog lives in component_dialog.js (shared with the invoice line flow);
// here we open it and, on success, go straight to the new component's detail page
// (mirrors the invoice flow). Only this standalone caller navigates — the invoice
// line and BOM "add to inventory" reuses stay put, since they create a component
// mid-task and must return to what the user was doing.
const newComponentBtn = document.getElementById("new-component-btn");
if (newComponentBtn && window.openComponentDialog) {
  newComponentBtn.addEventListener("click", () =>
    openComponentDialog(
      (created) => {
        window.location = `/components/${created.id}`;
      },
      null,
      { navigates: true }, // …so the dialog holds its afterword over the jump
    ),
  );
}

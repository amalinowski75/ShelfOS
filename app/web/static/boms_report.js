// BOM availability report (spec §21): fetch the live report feed and render the
// lines as a sortable, per-column-filterable Tabulator table. `esc` comes from
// shared.js. The feed (/api/boms/{id}/report) returns { bom, summary, lines[] };
// each line: references, value, category, mpn, quantity, status, stock,
// substitutes[] (component_id, mpn, package, value, stock, exact).
//
// The table renders every row up front (no height cap → no virtual DOM; see the
// Tabulator config below). Rows are kept single-line so that full render stays
// small and fast; substitute detail that doesn't fit on one line moves into the
// cell's hover tooltip.

// status → [badge class, label]. Labels are a fixed set (safe to inline).
const BOM_STATUS = {
  ok: ["b-ok", "in stock"],
  short: ["b-warn", "short"],
  out: ["b-danger", "out of stock"],
  missing: ["b-danger", "not in inventory"],
  no_mpn: ["b-neutral", "no MPN"],
};

function bomStatusFormatter(cell) {
  const [cls, label] = BOM_STATUS[cell.getValue()] || ["b-neutral", cell.getValue()];
  return `<span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span>`;
}

// A line has no matched stock unless it has an MPN; mirror the old template's "—".
function bomStockFormatter(cell) {
  return cell.getRow().getData().mpn ? String(cell.getValue()) : "—";
}

function bomMpnFormatter(cell) {
  const value = cell.getValue();
  return value
    ? `<span class="cell-mono">${esc(value)}</span>`
    : '<span class="muted">—</span>';
}

// Substitutes on one line: each value links to its component; `esc()` guards the
// value (it ultimately derives from an uploaded CSV). `Number()` neutralises the
// int fields. Full mpn/stock/exact detail is in the tooltip below.
function bomSubstitutesFormatter(cell) {
  const subs = cell.getValue() || [];
  if (!subs.length) return '<span class="muted">—</span>';
  const links = subs.map(
    (s) =>
      `<a class="cell-mono" href="/components/${Number(s.component_id)}">${esc(s.value)}</a>`,
  );
  return `<span class="subs-inline">${links.join(" · ")}</span>`;
}

// Plain-text tooltip (Tabulator sets it as a title attribute, so no escaping is
// needed) with each substitute's value, mpn, stock and whether it's an exact match.
function bomSubstitutesTooltip(subs) {
  return (subs || [])
    .map((s) => {
      const parts = [s.value];
      if (s.package) parts.push(s.package); // the candidate's footprint/package
      if (s.mpn) parts.push(s.mpn);
      parts.push(`stock ${Number(s.stock)}`);
      if (s.exact) parts.push("exact");
      return parts.join(" · ");
    })
    .join("\n");
}

function renderBomSummary(summary) {
  const el = document.getElementById("bom-summary");
  if (!el) return;
  const n = (v) => Number(v) || 0;
  el.innerHTML =
    `<p><strong>${n(summary.buildable)}</strong> buildable board(s) from exact MPN matches — ` +
    `${n(summary.ok)} in&nbsp;stock · ${n(summary.short)} short · ` +
    `${n(summary.out)} out · ${n(summary.missing)} not&nbsp;in&nbsp;inventory · ` +
    `${n(summary.no_mpn)} without&nbsp;MPN</p>`;
}

// A text header filter matching the app-wide pattern (placeholder + aria-label).
function bomTextFilter(title) {
  return {
    headerFilter: "input",
    headerFilterPlaceholder: `Filter ${title}…`,
    headerFilterParams: { elementAttributes: { "aria-label": `Filter ${title}` } },
  };
}

function bomReportColumns() {
  return [
    {
      title: "References",
      field: "references",
      width: 200,
      cssClass: "cell-mono",
      tooltip: true, // full refdes on hover; the cell truncates with an ellipsis
      ...bomTextFilter("References"),
    },
    { title: "Value", field: "value", ...bomTextFilter("Value") },
    { title: "Category", field: "category", ...bomTextFilter("Category") },
    {
      title: "Footprint",
      field: "footprint",
      cssClass: "cell-mono",
      ...bomTextFilter("Footprint"),
    },
    { title: "Qty", field: "quantity", width: 80, hozAlign: "right", sorter: "number" },
    {
      title: "MPN",
      field: "mpn",
      formatter: bomMpnFormatter,
      ...bomTextFilter("MPN"),
    },
    {
      title: "Status",
      field: "status",
      formatter: bomStatusFormatter,
      headerFilter: "list",
      headerFilterParams: {
        values: {
          "": "All",
          ok: "in stock",
          short: "short",
          out: "out of stock",
          missing: "not in inventory",
          no_mpn: "no MPN",
        },
      },
    },
    {
      title: "Stock",
      field: "stock",
      width: 90,
      hozAlign: "right",
      sorter: "number",
      formatter: bomStockFormatter,
    },
    {
      title: "Substitutes",
      field: "substitutes",
      headerSort: false,
      widthGrow: 2, // absorb the slack width under fitColumns
      minWidth: 160,
      formatter: bomSubstitutesFormatter,
      tooltip: (e, cell) => bomSubstitutesTooltip(cell.getValue()),
    },
  ];
}

async function loadReport(table, bomId) {
  let report;
  try {
    const resp = await fetch(`/api/boms/${bomId}/report`);
    if (!resp.ok) throw new Error();
    report = await resp.json();
  } catch {
    const el = document.getElementById("bom-summary");
    if (el) el.innerHTML = '<p class="error">Could not load the report.</p>';
    await table.setData([]); // clear the "No lines" placeholder — this is an error
    return;
  }
  renderBomSummary(report.summary);
  await table.setData(report.lines);
}

const bomTableEl = document.getElementById("bom-lines-table");
if (bomTableEl) {
  const bomId = bomTableEl.dataset.bomId;
  const table = new Tabulator("#bom-lines-table", {
    // Same config as the components table: fitColumns fits the container (no
    // horizontal scroll on desktop) and NO height cap — a fixed height turns on
    // Tabulator's virtual renderer, whose redraw on page-load/resize stalls for
    // tens of seconds. Rendering every row up front is instant for a 160-line BOM.
    layout: "fitColumns",
    placeholder: "No lines",
    columns: bomReportColumns(),
  });
  table.on("tableBuilt", () => loadReport(table, bomId));
}

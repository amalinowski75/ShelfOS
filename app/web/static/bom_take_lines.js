// The lines of one BOM take, as a Tabulator table fed by
// /web/api/bom-takes/{id}/lines. Same shape as the BOM report's own table — the
// two are read side by side, and a take of a 240-line board is exactly where
// sorting and per-column filters earn their keep.
//
// Every row renders up front (no height cap → no virtual DOM; see TABLE_DEFAULTS
// in shared.js), and frameTable gives the box its pixel height.

// A text header filter matching the app-wide pattern (placeholder + aria-label),
// the same one boms_report.js builds.
function takeTextFilter(title) {
  return {
    headerFilter: "input",
    headerFilterPlaceholder: `Filter ${title}…`,
    headerFilterParams: { elementAttributes: { "aria-label": `Filter ${title}` } },
  };
}

// The part number as it is today, linked to the component. The snapshot froze the
// id, which is the fact that matters; a part renamed since should still read as
// itself rather than as a number nobody recognises.
function takeComponentFormatter(cell) {
  const row = cell.getRow().getData();
  const id = Number(row.component_id);
  const label = esc(cell.getValue() || `#${id}`);
  return `<a class="cell-mono" href="/components/${id}">${label}</a>`;
}

// Short is the one number that carries news, so it reads as a badge rather than a
// digit in a column of digits. Zero says nothing worth marking.
function takeShortfallFormatter(cell) {
  const short = Number(cell.getValue()) || 0;
  if (!short) return "";
  return `<span class="badge b-warn"><span class="dot"></span>${short}</span>`;
}

function takeSourcesFormatter(cell) {
  const value = cell.getValue();
  return value ? esc(value) : '<span class="muted">—</span>';
}

// Tabulator sets tooltip content via innerHTML, so the value is escaped — a
// designator group and a location name both come from user input.
function takeCellTooltip(e, cell) {
  return esc(cell.getValue() ?? "");
}

function takeLinesColumns() {
  return [
    {
      title: "References",
      field: "references",
      width: 220,
      cssClass: "cell-mono",
      tooltip: takeCellTooltip, // the full designator group on hover
      ...takeTextFilter("References"),
    },
    {
      title: "Component",
      field: "mpn",
      width: 200,
      formatter: takeComponentFormatter,
      ...takeTextFilter("Component"),
    },
    {
      title: "Wanted",
      field: "requested",
      width: 110,
      hozAlign: "right",
      sorter: "number",
    },
    {
      title: "Taken",
      field: "taken",
      width: 100,
      hozAlign: "right",
      sorter: "number",
    },
    {
      title: "Short",
      field: "shortfall",
      width: 100,
      hozAlign: "right",
      sorter: "number",
      formatter: takeShortfallFormatter,
    },
    {
      title: "From",
      field: "from",
      minWidth: 220,
      formatter: takeSourcesFormatter,
      tooltip: takeCellTooltip,
      ...takeTextFilter("From"),
    },
  ];
}

async function loadTakeLines(table, takeId) {
  let rows;
  try {
    const resp = await fetch(`/web/api/bom-takes/${takeId}/lines`);
    if (!resp.ok) throw new Error();
    rows = await resp.json();
  } catch {
    // The placeholder says "no lines", which a failed load is not. Say which it
    // is, in the table's own frame.
    table.options.placeholder = "Could not load this take's lines.";
    await table.setData([]);
    frameTable(table);
    return false;
  }
  await table.setData(rows);
  frameTable(table);
  return true;
}

const takeLinesEl = document.getElementById("take-lines-table");
if (takeLinesEl) {
  const takeId = takeLinesEl.dataset.takeId;
  const takeTable = new Tabulator("#take-lines-table", {
    ...TABLE_DEFAULTS,
    layout: "fitDataFill",
    placeholder: "No lines",
    columns: takeLinesColumns(),
  });
  takeTable.on("tableBuilt", () => loadTakeLines(takeTable, takeId));
}

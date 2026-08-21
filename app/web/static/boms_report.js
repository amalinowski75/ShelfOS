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
  const badge = `<span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span>`;
  // "Short" is the one status where the count can carry news: there IS stock, just
  // not enough for the run — so answer the next question, "how many boards WILL
  // these make?". Only when that's a real number, though: at one board (the default
  // view) short means the stock doesn't even cover a single one, so the count is
  // always 0 — which says nothing the badge didn't, and worse, reads like a stock
  // figure. Out/missing are zero by definition and no-MPN was never matched.
  const boards = Number(cell.getRow().getData().boards_possible) || 0;
  if (cell.getValue() === "short" && boards > 0) {
    return `${badge} <span class="muted">enough for ${boards}</span>`;
  }
  return badge;
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

// Tooltip with each substitute's value, footprint, mpn, stock and exact-ness.
// Tabulator sets tooltip content via innerHTML (NOT a title attribute), so every
// user-controlled field must be esc()'d — value/package/mpn derive from an
// uploaded CSV / free-text component fields. Ints go through Number().
function bomSubstitutesTooltip(subs) {
  return (subs || [])
    .map((s) => {
      const parts = [esc(s.value)];
      if (s.package) parts.push(esc(s.package)); // the candidate's footprint/package
      if (s.mpn) parts.push(esc(s.mpn));
      parts.push(`stock ${Number(s.stock)}`);
      if (s.exact) parts.push("exact");
      return parts.join(" · ");
    })
    .join("\n");
}

// References tooltip: Tabulator would innerHTML the raw cell value for
// `tooltip: true`, so escape it (the refdes string comes from the CSV).
function bomReferencesTooltip(e, cell) {
  return esc(cell.getValue() ?? "");
}

function renderBomSummary(summary) {
  const el = document.getElementById("bom-summary");
  if (!el) return;
  const n = (v) => Number(v) || 0;
  // Buildable is what the stock covers; when a longer run was asked for, say so —
  // "3 of 10" is the answer to the question the Boards box just posed.
  const boards = n(summary.boards) || 1;
  const of = boards > 1 ? ` of ${boards} requested` : "";
  el.innerHTML =
    `<p><strong>${n(summary.buildable)}</strong> buildable board(s)${of} from exact MPN matches — ` +
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
      tooltip: bomReferencesTooltip, // full refdes on hover; escaped (see helper)
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
    // Two figures: what one board takes, and what the whole run takes. "Total"
    // tracks the Boards control; with one board the two columns agree.
    {
      title: "Qty/board",
      field: "quantity",
      width: 125, // fits the header next to its sort arrow
      hozAlign: "right",
      sorter: "number",
    },
    {
      title: "Total",
      field: "total_quantity",
      width: 90,
      hozAlign: "right",
      sorter: "number",
    },
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
      minWidth: 160,
      formatter: bomSubstitutesFormatter,
      tooltip: (e, cell) => bomSubstitutesTooltip(cell.getValue()),
    },
  ];
}

// How many boards the report is for. Kept per BOM in localStorage rather than in
// the database: it's a view of the same stored BOM, and the number you're building
// today shouldn't rewrite shared state.
const BOM_BOARDS_KEY = (bomId) => `shelfos:bom-boards:${bomId}`;

function bomBoardsValue(bomId) {
  const input = document.getElementById("bom-boards");
  const raw = input ? Number(input.value) : Number(bomBoardsStored(bomId));
  return Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : 1;
}

function bomBoardsStored(bomId) {
  try {
    return Number(localStorage.getItem(BOM_BOARDS_KEY(bomId))) || 1;
  } catch {
    return 1; // private mode: just build one board
  }
}

function bomBoardsRemember(bomId, boards) {
  try {
    localStorage.setItem(BOM_BOARDS_KEY(bomId), String(boards));
  } catch {
    /* not worth breaking the page over */
  }
}

async function loadReport(table, bomId) {
  let report;
  const boards = bomBoardsValue(bomId);
  try {
    const resp = await fetch(`/api/boms/${bomId}/report?boards=${boards}`);
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
  frameTable(table);
}

// A line can be turned into a new inventory component when nothing matches it:
// a missing MPN, or no MPN yet (still designing).
function bomCanAdd(status) {
  return status === "missing" || status === "no_mpn";
}

// Seed the "New component" dialog from a BOM line. Only passives carry a numeric
// value worth pre-filling; a part-number "value" (IC/transistor) is left out.
// The line's footprint is intentionally NOT mapped to the Package field — a KiCad
// footprint ("R_0402_1005Metric") isn't the package name ("0402").
const BOM_PASSIVE = new Set(["resistor", "capacitor", "inductor"]);
function bomAddPrefill(row) {
  return {
    category: row.category || null,
    value: BOM_PASSIVE.has(row.category) ? row.value || null : null,
    mpn: row.mpn || null,
    manufacturer: row.manufacturer || null,
  };
}

// The component-detail URL for a matched line (ok/short/out → a matched part), or
// null when nothing is in inventory (missing/no_mpn) so the row isn't clickable.
function bomRowTarget(row) {
  const matched = row.matched && row.matched[0];
  return matched && matched.component_id
    ? `/components/${matched.component_id}`
    : null;
}

const bomTableEl = document.getElementById("bom-lines-table");
if (bomTableEl) {
  const bomId = bomTableEl.dataset.bomId;
  const table = new Tabulator("#bom-lines-table", {
    // Natural column widths + a horizontal scrollbar when they overflow; framed to
    // a sticky-header scroll box by frameTable (fixed px height — never vh/maxHeight,
    // which freeze Tabulator on resize; see shared.js frameTable).
    layout: "fitDataFill",
    placeholder: "No lines",
    columns: bomReportColumns(),
    // A matched line reads as clickable (→ its component); unmatched rows don't.
    rowFormatter: (row) => {
      if (bomRowTarget(row.getData())) row.getElement().style.cursor = "pointer";
    },
  });

  // Clicking a matched line opens its component detail page. Ignore clicks on the
  // substitute links and the "Add to inventory" button so those still act.
  table.on("rowClick", (e, row) => {
    if (e.target.closest("a, button")) return;
    const url = bomRowTarget(row.getData());
    if (url) window.location = url;
  });

  // Writers get an "Add to inventory" action on unmatched lines, opening the
  // shared New Component dialog pre-filled from the line; refresh on success so
  // the line resolves. (Read-only users never see it; the API is writer-gated.)
  if (canWrite) {
    table.on("tableBuilt", () =>
      table.addColumn({
        title: "",
        field: "_add",
        headerSort: false,
        width: 150,
        hozAlign: "right",
        formatter: (cell) =>
          bomCanAdd(cell.getRow().getData().status)
            ? '<button class="btn btn-secondary btn-sm" data-act="add-component">Add to inventory</button>'
            : "",
        cellClick: (e, cell) => {
          if (e.target.dataset.act !== "add-component") return;
          if (window.openComponentDialog) {
            openComponentDialog(
              () => loadReport(table, bomId),
              bomAddPrefill(cell.getRow().getData()),
            );
          }
        },
      }),
    );
  }

  // Boards: restore the remembered count before the first load, then reload the
  // report on every change (the multiplier is applied server-side).
  const boardsInput = document.getElementById("bom-boards");
  if (boardsInput) {
    boardsInput.value = String(bomBoardsStored(bomId));
    boardsInput.addEventListener("change", () => {
      const boards = bomBoardsValue(bomId);
      boardsInput.value = String(boards); // snap 0/blank/2.5 back to a real count
      bomBoardsRemember(bomId, boards);
      loadReport(table, bomId);
    });
  }

  // "Reload from CSV": re-run the import-time parse over the stored file, then
  // re-read the report. Stock matching is already live, so only the parsed fields
  // change — which is why this says what it did rather than looking like a no-op.
  const reloadBtn = document.getElementById("bom-reload");
  const reloadStatus = document.getElementById("bom-reload-status");
  if (reloadBtn) {
    let reloading = false;
    reloadBtn.addEventListener("click", () => {
      if (reloading) return;
      reloading = true;
      reloadBtn.disabled = true;
      const say = (text, isError) => {
        if (!reloadStatus) return;
        reloadStatus.textContent = text;
        reloadStatus.className = isError ? "error" : "muted";
        reloadStatus.hidden = false;
      };
      say("Re-reading the stored CSV…");
      (async () => {
        try {
          const resp = await fetch(`/api/boms/${bomId}/reimport`, {
            method: "POST",
            headers: { "X-CSRF-Token": csrfToken },
          });
          if (resp.ok) {
            await loadReport(table, bomId);
            say("Lines rebuilt from the stored CSV.");
          } else {
            say(await errorMessage(resp), true);
          }
        } catch {
          say("Could not reach the server.", true);
        } finally {
          reloading = false;
          reloadBtn.disabled = false;
        }
      })();
    });
  }

  table.on("tableBuilt", () => loadReport(table, bomId));
}

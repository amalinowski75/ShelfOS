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

// "—" means "nothing was looked up", not "zero": a line with no MPN has no match to
// count. An ASSIGNED line always has one, whatever its MPN says — so it shows a
// real number, including 0.
function bomStockFormatter(cell) {
  const row = cell.getRow().getData();
  return row.mpn || row.assigned ? String(cell.getValue()) : "—";
}

function bomMpnFormatter(cell) {
  const value = cell.getValue();
  return value
    ? `<span class="cell-mono">${esc(value)}</span>`
    : '<span class="muted">—</span>';
}

// The component someone assigned to this line, if any. Kept in its own column so
// the CSV's MPN stays visible beside it — "what the file says" and "what we build
// it from" are different facts, and comparing them is the point.
function bomAssignedFormatter(cell) {
  const assigned = cell.getValue();
  if (!assigned) return '<span class="muted">—</span>';
  const label = esc(assigned.mpn || `#${assigned.component_id}`);
  const link = `<a class="cell-mono" href="/components/${Number(assigned.component_id)}">${label}</a>`;
  // A part retired after it was assigned still shows — silently dropping the
  // assignment would leave the line looking untouched.
  return assigned.deleted
    ? `${link} <span class="badge b-danger"><span class="dot"></span>not in use</span>`
    : link;
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
    // "matched and assigned", not "exact MPN matches": an assigned line feeds this
    // number too, and it may have no MPN at all — the headline can't claim a kind of
    // match the figure no longer comes only from.
    `<p><strong>${n(summary.buildable)}</strong> buildable board(s)${of} from matched and assigned parts — ` +
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
      title: "Assigned",
      field: "assigned",
      headerSort: false,
      formatter: bomAssignedFormatter,
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
      // Next to Status: together they read as "where this line stands" — what the
      // shelf says, and what has been done about it.
      title: "Ordered",
      field: "ordered",
      width: 100,
      hozAlign: "center",
      // Marks the whole cell, not just the box: the row-click guard uses it, so a
      // click that lands beside the checkbox doesn't navigate away from the report.
      cssClass: "bom-ordered-cell",
      formatter: bomOrderedFormatter,
      headerFilter: "tickCross",
      headerFilterParams: { tristate: true },
      headerFilterEmptyCheck: (value) => value === null,
      cellClick: (e, cell) => {
        if (e.target.dataset.act !== "ordered") return;
        // The box has already flipped itself; persist what it now shows, and hold
        // the row's data in step so a later redraw doesn't undo it.
        const ordered = e.target.checked;
        const row = cell.getRow().getData();
        bomSetOrdered(bomTableEl.dataset.bomId, row.id, ordered, e.target).then(
          (saved) => {
            if (saved) cell.getRow().update({ ordered });
          },
        );
      },
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
  // Assigning a component, ticking Ordered off a refresh, adding to inventory — all
  // of them reload this table, and setData scrolls it back to the top. On a BOM of
  // any size that means hunting for the line you were just on, every single time.
  // Put the scroll back where it was instead. Read BEFORE setData, restored after
  // frameTable, which resizes the table and would undo it.
  const holder = table.element?.querySelector?.(".tabulator-tableholder");
  const scrollTop = holder ? holder.scrollTop : 0;
  await table.setData(report.lines);
  frameTable(table);
  if (holder && scrollTop) restoreScroll(holder, scrollTop);
}

// Tabulator renders rows asynchronously, so the height the scroll needs may not
// exist yet the moment setData resolves. Set it now for the common case, and once
// more on the next turn for the case where it didn't take — a scrollTop set against
// a container that is still short is silently clamped.
function restoreScroll(holder, scrollTop) {
  holder.scrollTop = scrollTop;
  setTimeout(() => {
    if (holder.scrollTop !== scrollTop) holder.scrollTop = scrollTop;
  }, 0);
}

// A line can be turned into a new inventory component when nothing matches it:
// a missing MPN, or no MPN yet (still designing).
function bomCanAdd(status) {
  return status === "missing" || status === "no_mpn";
}

// The per-line actions. "Assign" is offered on EVERY line, not just an unmatched
// one: a line that matches its MPN can still be built from something else, and
// that's a decision the CSV has no way to carry.
function bomActionButtons(row) {
  const buttons = [];
  if (bomCanAdd(row.status) && !row.assigned) {
    buttons.push(
      '<button class="btn btn-secondary btn-sm" data-act="add-component">Add to inventory</button>',
    );
  }
  buttons.push(
    `<button class="btn btn-secondary btn-sm" data-act="assign-component">${
      row.assigned ? "Change" : "Assign…"
    }</button>`,
  );
  if (row.assigned) {
    buttons.push(
      '<button class="btn btn-ghost btn-sm" data-act="unassign-component">Remove</button>',
    );
  }
  // NOT `.row-actions`: that class is hover-only (app.css), which would hide the
  // very action this feature is about until you happened to point at the row.
  return `<div class="bom-row-actions">${buttons.join("")}</div>`;
}

// "Ordered" is a note the user keeps, not something the report can work out, so it
// is the one editable cell here. Read-only accounts see the state without a control
// they can't use (the endpoint is writer-gated anyway).
function bomOrderedFormatter(cell) {
  const on = cell.getValue() ? " checked" : "";
  if (!canWrite) return cell.getValue() ? "✓" : '<span class="muted">—</span>';
  return (
    `<input type="checkbox" data-act="ordered"${on}` +
    ` aria-label="Ordered — ${esc(cell.getRow().getData().references || "")}">`
  );
}

// Lines with a tick in flight. Two fast clicks on one box would otherwise send two
// PUTs, and since the row's data is updated per resolution, what is STORED (the
// last to land) and what is SHOWN (the last to resolve) could disagree. Every other
// write on this page guards; this one should too.
const bomOrderedInFlight = new Set();

async function bomSetOrdered(bomId, lineId, ordered, checkbox) {
  if (bomOrderedInFlight.has(lineId)) {
    if (checkbox) checkbox.checked = !ordered; // undo the click we're dropping
    return false;
  }
  bomOrderedInFlight.add(lineId);
  try {
    const resp = await fetch(`/api/boms/${bomId}/lines/${lineId}/ordered`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ ordered }),
    });
    if (resp.ok) return true;
    alert(await errorMessage(resp));
  } catch {
    alert("Could not reach the server.");
  } finally {
    // Released on EVERY path, success included — a lock left set would make the
    // box dead for the rest of the visit.
    bomOrderedInFlight.delete(lineId);
  }
  // Put the box back the way the server still has it, rather than leaving the tick
  // showing a state that was never stored.
  if (checkbox) checkbox.checked = !ordered;
  return false;
}

async function bomUnassign(bomId, lineId, onDone) {
  try {
    const resp = await fetch(`/api/boms/${bomId}/lines/${lineId}/component`, {
      method: "DELETE",
      headers: { "X-CSRF-Token": csrfToken },
    });
    if (!resp.ok) {
      alert(await errorMessage(resp));
      return;
    }
  } catch {
    alert("Could not reach the server.");
    return;
  }
  if (onDone) await onDone();
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
  // controls inside it — substitute links, the row's buttons, and the Ordered
  // checkbox — so ticking a box doesn't navigate away from the report. The whole
  // Ordered CELL is excluded, not just the box: that cell exists to host a control,
  // so missing it by a pixel should cost nothing, never the page.
  table.on("rowClick", (e, row) => {
    if (e.target.closest("a, button, input, .bom-ordered-cell")) return;
    const url = bomRowTarget(row.getData());
    if (url) window.location = url;
  });

  // Writers get the per-line actions: "Add to inventory" for a line nothing in
  // stock answers, and Assign/Change/Remove for pointing a line at a component
  // whatever its MPN says. (Read-only users never see them; the API is
  // writer-gated.)
  if (canWrite) {
    table.on("tableBuilt", () =>
      table.addColumn({
        title: "",
        field: "_actions",
        headerSort: false,
        width: 260,
        hozAlign: "right",
        formatter: (cell) => bomActionButtons(cell.getRow().getData()),
        cellClick: (e, cell) => {
          const act = e.target.dataset.act;
          const row = cell.getRow().getData();
          const reload = () => loadReport(table, bomId);
          if (act === "add-component") {
            if (window.openComponentDialog) {
              openComponentDialog(reload, bomAddPrefill(row));
            }
          } else if (act === "assign-component") {
            window.openBomPicker?.(row, bomId, reload);
          } else if (act === "unassign-component") {
            bomUnassign(bomId, row.id, reload);
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

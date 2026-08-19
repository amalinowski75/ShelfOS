// Match-rules management (admin only). A Tabulator over the /web/api/match-rules
// feed: every column sorts and filters, alias/target/order edit in place (PATCH),
// and a create dialog adds rules — scoped domains (param_name, enum_value) also pick
// the parameter they apply to. Writes go through /api/admin/match-rules… (admin +
// CSRF). `csrfToken`, `esc`, `errorMessage` and `frameTable` come from shared.js.

// Existing type names, cached for the in-place Target editor of a type rule (the
// list is fetched once at load; the create dialog refreshes it when it opens).
let cachedTypeNames = [];

// The MountingType values, read from the create dialog's select so the template
// stays the single source of truth for the enum.
function mountingValues() {
  const select = document.querySelector('#rule-new-form [name="canonical_mounting"]');
  return select ? [...select.options].map((o) => o.value) : [];
}

async function loadTypeNames() {
  try {
    const types = await fetch("/api/types").then((r) => r.json());
    cachedTypeNames = types.map((t) => t.name);
  } catch {
    cachedTypeNames = [];
  }
}

// The in-place Target editor's options for a given row: a fixed list for the three
// domains with a vocabulary (mounting enum, existing types, an enum parameter's
// allowed values — shipped on the row), free text for a param_name or package target.
// This mirrors the create dialog, so a mistyped mounting/enum can't slip in through
// inline editing either. A package target has no vocabulary to constrain it — the
// shelf's own case names ("SOT-23", "DIP-8") are whatever the admin writes. NB:
// Tabulator's list editor only accepts typed text when `autocomplete` is on — a bare
// `freetext` is silently ignored and the cell becomes un-editable, so both are set.
function targetEditorParams(cell) {
  const row = cell.getRow().getData();
  if (row.domain === "mounting") return { values: mountingValues() };
  if (row.domain === "type") return { values: cachedTypeNames };
  if (row.domain === "enum_value") return { values: row.enum_values || [] };
  return { values: [], autocomplete: true, freetext: true, listOnEmpty: true };
}

async function sendRuleWrite(url, method, payload) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}

// Give a column the same live text header filter the other tables use: a
// case-insensitive substring match applied as you type, ANDed across columns.
function ruleFilter(column) {
  return {
    ...column,
    headerFilter: "input",
    headerFilterPlaceholder: `Filter ${column.title}…`,
    headerFilterParams: {
      elementAttributes: { "aria-label": `Filter ${column.title}` },
    },
  };
}

// Push one inline cell edit to the API; on failure, revert the cell and say why —
// this is what surfaces the "alias already used" guard instead of silently keeping
// a value the server rejected.
async function saveCellEdit(cell, field) {
  const row = cell.getRow().getData();
  const value = field === "sort_order" ? Number(cell.getValue()) : cell.getValue();
  try {
    const resp = await sendRuleWrite(
      `/api/admin/match-rules/${row.id}`,
      "PATCH",
      { [field]: value },
    );
    if (resp.ok) {
      // Reload so a server-normalised value (e.g. a mounting target folded to its
      // exact enum spelling) is what the row shows, not the raw text just typed.
      await loadRules();
    } else {
      alert(await errorMessage(resp));
      cell.restoreOldValue();
    }
  } catch {
    alert("Could not reach the server.");
    cell.restoreOldValue();
  }
}

function ruleColumns() {
  return [
    // Domain and Parameter identify the rule and aren't editable in place (change
    // them by deleting and re-adding) — but both still sort and filter.
    ruleFilter({ title: "Domain", field: "domain", width: 140 }),
    ruleFilter({
      title: "Alias",
      field: "alias",
      editor: "input",
      cellEdited: (cell) => saveCellEdit(cell, "alias"),
      formatter: (cell) => `<span class="cell-mono">${esc(cell.getValue())}</span>`,
    }),
    ruleFilter({
      title: "Target",
      field: "canonical",
      // A list of the domain's valid values (mounting enum / existing types), or
      // free text for a param_name or package rule — chosen by targetEditorParams.
      editor: "list",
      editorParams: targetEditorParams,
      cellEdited: (cell) => saveCellEdit(cell, "canonical"),
      formatter: (cell) => `<span class="cell-mono">${esc(cell.getValue())}</span>`,
    }),
    ruleFilter({
      title: "Parameter",
      field: "parameter",
      formatter: (cell) => esc(cell.getValue() || ""),
    }),
    ruleFilter({
      title: "Order",
      field: "sort_order",
      width: 100,
      hozAlign: "right",
      sorter: "number",
      editor: "number",
      // The engine takes the first matching alias, so lower wins when several aliases
      // that could match the same text share a scope — type/mounting/package
      // globally, and an enum_value parameter's aliases within that parameter.
      headerTooltip:
        "Lower wins when several aliases that could match the same text share a " +
        "scope — e.g. 'led' before 'diode' for types, 'SOT-23' before 'SOT-23-3' " +
        "for packages, or two aliases of the same enum parameter.",
      cellEdited: (cell) => saveCellEdit(cell, "sort_order"),
    }),
    {
      title: "",
      field: "actions",
      headerSort: false,
      width: 110,
      hozAlign: "right",
      formatter: () =>
        `<div class="row-actions">
           <button class="btn btn-ghost btn-sm" data-act="delete">Delete</button>
         </div>`,
      cellClick: (event, cell) => {
        if (event.target.dataset.act === "delete") deleteRule(cell.getRow().getData());
      },
    },
  ];
}

const rulesTable = new Tabulator("#rules-table", {
  ...TABLE_DEFAULTS,
  layout: "fitDataFill",
  placeholder: "No match rules",
  columns: ruleColumns(),
});

async function loadRules() {
  let rows = [];
  let failed = false;
  try {
    // Checking the SHAPE, not resp.ok: an HTTP failure body parses as JSON
    // perfectly well and simply has no `data` array, so this catches a 404 as
    // surely as a 200 of the wrong shape — where a resp.ok test would catch
    // strictly less and read as if it caught more.
    const body = await fetch("/web/api/match-rules").then((r) => r.json());
    if (!Array.isArray(body.data)) throw new Error("unexpected response");
    rows = body.data;
  } catch {
    failed = true;
    alert("Could not load match rules — refresh to try again.");
  }
  // A failure is never left looking like an empty vocabulary: the alert is
  // transient, the placeholder is what stays on the screen.
  rulesTable.options.placeholder = failed
    ? "Could not load match rules"
    : "No match rules";
  try {
    await rulesTable.setData(rows);
  } finally {
    frameTable(rulesTable);
  }
}

// Ignore a re-entrant submit while a write is in flight (stops a double-click
// sending a duplicate request).
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

const guardDelete = makeGuard();
function deleteRule(row) {
  if (!confirm(`Delete rule "${row.alias}" → "${row.canonical}"?`)) return;
  guardDelete(async () => {
    try {
      const resp = await sendRuleWrite(
        `/api/admin/match-rules/${row.id}`,
        "DELETE",
      );
      if (resp.ok) await loadRules();
      else alert(await errorMessage(resp));
    } catch {
      alert("Could not reach the server.");
    }
  });
}

// The create dialog lives in match_rule_dialog.js (shared so the type builder and a
// type's parameter list can open it too); wire the admin "New rule" button to it.
document.getElementById("rule-new-btn")?.addEventListener("click", () => {
  window.openMatcherDialog?.(loadRules);
});

rulesTable.on("tableBuilt", loadRules);
loadTypeNames(); // ready the type list for the inline Target editor

// ---- manufacturer aliases --------------------------------------------------
// The other half of "when you see this, treat it as that": one spelling of a
// maker's name per row, and the name components are stored under. Written by the
// import dialog when someone answers "this is it"; removable only here, which is
// the point — until this table existed a mistaken alias could not be undone from
// the app at all, not even by editing the component, since the write path
// canonicalises too.


const aliasesTable = new Tabulator("#aliases-table", {
  ...TABLE_DEFAULTS,
  // fitColumns, not the fitDataFill the rules table uses: three columns of short
  // names size to a few characters each and huddle at the left edge of a wide page,
  // which reads as a broken table rather than a small one.
  layout: "fitColumns",
  placeholder: "No manufacturer aliases",
  columns: [
    ruleFilter({ title: "Seen as", field: "alias", widthGrow: 1 }),
    ruleFilter({ title: "Stored as", field: "canonical", widthGrow: 1 }),
    {
      title: "",
      field: "actions",
      headerSort: false,
      width: 110,
      hozAlign: "right",
      // aria-label names the row: every button on the page otherwise reads as the
      // bare word "Forget", which tells a screen-reader user nothing about which
      // spelling they are about to drop.
      formatter: (cell) =>
        '<button class="btn btn-ghost btn-sm" data-act="forget" aria-label="Forget ' +
        `“${esc(cell.getRow().getData().alias)}”">Forget</button>`,
      cellClick: (event, cell) => {
        if (event.target.dataset.act !== "forget") return;
        forgetAlias(cell.getRow().getData());
      },
    },
  ],
});

const aliasGuard = makeGuard();

function forgetAlias(row) {
  // Named for what it does: later imports stop resolving that spelling. Say so,
  // because "delete" next to a manufacturer's name reads as though it might rename
  // or remove the parts filed under it, and it does neither.
  const ok = window.confirm(
    `Forget that “${row.alias}” means “${row.canonical}”?\n\n` +
      "Components already stored under that name keep it — only later imports change.",
  );
  if (!ok) return;
  aliasGuard(async () => {
    try {
      const resp = await sendRuleWrite(
        `/api/manufacturers/aliases/${row.id}`,
        "DELETE",
      );
      if (resp.ok) return loadAliases();
      alert(await errorMessage(resp));
    } catch {
      alert("Could not reach the server.");
    }
  });
}

async function loadAliases() {
  let rows = [];
  let failed = false;
  try {
    // The check is on the SHAPE, and it is the only one needed: an HTTP failure
    // body parses as JSON perfectly well and simply has no `data` array, so this
    // catches a 404 or a 500 as surely as a 200 of the wrong shape — where a
    // `resp.ok` test would catch strictly less and read as if it caught more.
    //
    // And a failure is a failure, never an empty list: showing an empty table
    // nobody was warned about answers "you have no aliases", which is a worse
    // thing to be wrong about than "could not load". (Found the hard way, against
    // a server left running from an earlier session.)
    const body = await fetch("/web/api/manufacturer-aliases").then((r) => r.json());
    if (!Array.isArray(body.data)) throw new Error("unexpected response");
    rows = body.data;
  } catch {
    failed = true;
    alert("Could not load manufacturer aliases — refresh to try again.");
  }
  // The placeholder is the ONE empty state. The note above the table already
  // explains where aliases come from whether or not there are any, so a third
  // sentence saying it again under an empty table earns nothing — and leaving that
  // sentence on screen after a failed load would state as fact the very thing this
  // function refuses to claim: that you have no aliases.
  aliasesTable.options.placeholder = failed
    ? "Could not load manufacturer aliases"
    : "No manufacturer aliases yet";
  try {
    await aliasesTable.setData(rows);
  } finally {
    // NOT frameTable on THIS table: it sizes to its content. frameTable fills the
    // rest of the viewport, which only one table on a page can do — and the rules
    // table above is the one that can be long. Re-frame that one instead, now that
    // this table has its height: frameTable measures the live page bottom, so it
    // has to run after everything below it is on the page, or it leaves room for a
    // table that was not there yet. In a `finally`, so a table that fails to take
    // its data cannot leave the other one unframed.
    frameTable(rulesTable);
  }
}

aliasesTable.on("tableBuilt", loadAliases);

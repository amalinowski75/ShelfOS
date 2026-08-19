// Match-rules management (admin only). A Tabulator over the /web/api/match-rules
// feed: every column sorts and filters, alias/target/order edit in place (PATCH),
// and a create dialog adds rules — scoped domains (param_name, enum_value) also pick
// the parameter they apply to. Writes go through /api/admin/match-rules… (admin +
// CSRF). `csrfToken`, `esc`, `errorMessage` and `frameTable` come from shared.js.
//
// ONE ROW PER TARGET, not per rule: the feed's rules are grouped by (domain, target,
// scope) and their aliases shown as one comma-separated list, so a target's whole
// vocabulary is read and edited in one field. The engine still holds a rule per alias
// — the grouping is `groupRulesByTarget` in shared.js, and every edit below writes to
// each rule the row stands for.

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
// allowed values — shipped on the row), free text only for a param_name target.
// This mirrors the create dialog, so a mistyped mounting/enum can't slip in through
// inline editing either. NB: Tabulator's list editor only accepts typed text when
// `autocomplete` is on — a bare `freetext` is silently ignored and the cell becomes
// un-editable, so param_name sets both.
function targetEditorParams(cell) {
  const row = cell.getRow().getData();
  if (row.domain === "mounting") return { values: mountingValues() };
  if (row.domain === "type") return { values: cachedTypeNames };
  if (row.domain === "enum_value") return { values: row.enum_values || [] };
  return { values: [], autocomplete: true, freetext: true, listOnEmpty: true };
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

// Push an inline Target/Order edit to every rule the row stands for — both fields
// belong to the target, not to one of its aliases, so they move together. On failure,
// revert the cell and say why: this is what surfaces the server's guards instead of
// silently keeping a value it rejected.
async function saveCellEdit(cell, field) {
  const row = cell.getRow().getData();
  const value = field === "sort_order" ? Number(cell.getValue()) : cell.getValue();
  const failure = await runMatchRuleWrites(
    row.rules.map((rule) => ({
      method: "PATCH",
      id: rule.id,
      body: { [field]: value },
    })),
  );
  // Reload either way: a server-normalised value (a mounting target folded to its
  // exact enum spelling) must be what the row shows, and a run that stopped partway
  // leaves the earlier rules already written.
  await loadRules();
  if (failure) alert(failure);
}

// Save an edited alias list: the field holds the target's whole vocabulary, so the
// diff against what the row already had becomes renames, removals and additions.
async function saveAliasList(cell) {
  const row = cell.getRow().getData();
  const writes = aliasListWrites(row, cell.getValue());
  if (!writes) {
    alert("A target needs at least one alias — use Delete to remove it entirely.");
    cell.restoreOldValue();
    return;
  }
  const failure = await runMatchRuleWrites(writes);
  await loadRules();
  if (failure) alert(failure);
}

function ruleColumns() {
  return [
    // Domain and Parameter identify the rule and aren't editable in place (change
    // them by deleting and re-adding) — but both still sort and filter.
    ruleFilter({ title: "Domain", field: "domain", width: 140 }),
    ruleFilter({
      // Every alias for this target, comma-separated: what is typed here IS the set,
      // so removing one from the list removes its rule.
      title: "Aliases",
      field: "alias",
      editor: "input",
      cellEdited: saveAliasList,
      formatter: (cell) => `<span class="cell-mono">${esc(cell.getValue())}</span>`,
    }),
    ruleFilter({
      title: "Target",
      field: "canonical",
      // A list of the domain's valid values (mounting enum / existing types), or
      // free text for a parameter rule — chosen per row by targetEditorParams.
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
      // that could match the same text share a scope — type/mounting globally, and an
      // enum_value parameter's aliases within that parameter.
      headerTooltip:
        "Lower wins when several aliases that could match the same text share a " +
        "scope — e.g. 'led' before 'diode' for types, or two aliases of the same " +
        "enum parameter.",
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
  layout: "fitDataFill",
  placeholder: "No match rules",
  columns: ruleColumns(),
});

async function loadRules() {
  try {
    const payload = await fetch("/web/api/match-rules").then((r) => r.json());
    await rulesTable.setData(groupRulesByTarget(payload.data));
    frameTable(rulesTable);
  } catch {
    await rulesTable.setData([]);
    frameTable(rulesTable);
    alert("Could not load match rules — refresh to try again.");
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
// Delete the whole target — every alias the row lists. The prompt counts them, since
// one row can stand for a dozen rules and the button gives no other hint of that.
function deleteRule(row) {
  const count = row.rules.length;
  const what = count === 1 ? `"${row.alias}"` : `all ${count} aliases`;
  if (!confirm(`Delete ${what} → "${row.canonical}"?`)) return;
  guardDelete(async () => {
    const failure = await runMatchRuleWrites(
      row.rules.map((rule) => ({ method: "DELETE", id: rule.id })),
    );
    await loadRules();
    if (failure) alert(failure);
  });
}

// The create dialog lives in match_rule_dialog.js (shared so the type builder and a
// type's parameter list can open it too); wire the admin "New rule" button to it.
document.getElementById("rule-new-btn")?.addEventListener("click", () => {
  window.openMatcherDialog?.(loadRules);
});

rulesTable.on("tableBuilt", loadRules);
loadTypeNames(); // ready the type list for the inline Target editor

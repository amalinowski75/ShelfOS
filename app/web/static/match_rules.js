// Match-rules management (admin only). A Tabulator over the /web/api/match-rules
// feed: every column sorts and filters, alias/target/order edit in place (PATCH),
// and a create dialog adds rules — scoped domains (param_name, enum_value) also pick
// the parameter they apply to. Writes go through /api/admin/match-rules… (admin +
// CSRF). `csrfToken`, `esc`, `errorMessage` and `frameTable` come from shared.js.

// The two domains that attach a rule to a single parameter definition.
const SCOPED_DOMAINS = new Set(["param_name", "enum_value"]);

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

// The in-place Target editor's options for a given row: a fixed list for the
// domains with a vocabulary (mounting enum, existing types), free text otherwise —
// the same constraint the create dialog applies, so a mistyped mounting can't slip
// in through inline editing either.
function targetEditorParams(cell) {
  const domain = cell.getRow().getData().domain;
  if (domain === "mounting") return { values: mountingValues() };
  if (domain === "type") return { values: cachedTypeNames };
  return { values: [], freetext: true };
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
      // Order only matters within a domain (type/mounting), where the engine takes
      // the first matching alias — lower wins when several could match the same text.
      headerTooltip:
        "Lower wins when several aliases in the same domain (type/mounting) " +
        "could match the same text — e.g. 'led' before 'diode'. Ignored for " +
        "parameter rules.",
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
    await rulesTable.setData(payload.data);
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

// --- create ---
const newRuleBtn = document.getElementById("rule-new-btn");
if (newRuleBtn) {
  const dialog = document.getElementById("rule-new-dialog");
  const form = document.getElementById("rule-new-form");
  const error = document.getElementById("rule-new-error");
  const typeField = document.getElementById("rule-scope-type");
  const paramField = document.getElementById("rule-scope-param");
  const typeSelect = form.elements.type;
  const paramSelect = form.elements.parameter;
  // The three possible "Target" controls; only the one matching the domain shows.
  const targetTypeSelect = form.elements.canonical_type; // type rule → a type name
  const targetMountingSelect = form.elements.canonical_mounting; // mounting → enum
  const targetTextInput = form.elements.canonical_text; // param/enum → free text

  function isScoped() {
    return SCOPED_DOMAINS.has(form.elements.domain.value);
  }

  // Reflect the chosen domain: show its scope pickers (param/enum only) and the one
  // Target control that fits — an existing-type list, the mounting enum, or free
  // text — so a bad target (e.g. a mistyped mounting) can't be entered.
  async function syncFields() {
    const domain = form.elements.domain.value;
    const scoped = isScoped();
    typeField.hidden = !scoped;
    paramField.hidden = !scoped;
    targetTypeSelect.hidden = domain !== "type";
    targetMountingSelect.hidden = domain !== "mounting";
    targetTextInput.hidden = scoped === false && domain !== "type";
    // The type list feeds both the scope picker and the type-target select, so load
    // it whenever either needs it.
    if ((scoped || domain === "type") && typeSelect.options.length === 0) {
      await loadTypes();
    }
  }

  async function loadTypes() {
    try {
      const types = await fetch("/api/types").then((r) => r.json());
      // Scope picker keys by id (which parameter's owner); the target select stores
      // the type NAME, which is what a type rule's canonical is matched against.
      typeSelect.innerHTML = types
        .map((t) => `<option value="${t.id}">${esc(t.name)}</option>`)
        .join("");
      targetTypeSelect.innerHTML = types
        .map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`)
        .join("");
      cachedTypeNames = types.map((t) => t.name); // keep the inline editor in sync
      await loadParams();
    } catch {
      error.textContent = "Could not load types.";
      error.hidden = false;
    }
  }

  async function loadParams() {
    const typeId = typeSelect.value;
    if (!typeId) {
      paramSelect.innerHTML = "";
      return;
    }
    try {
      const params = await fetch(`/api/types/${typeId}/parameters`).then((r) =>
        r.json(),
      );
      paramSelect.innerHTML = params
        .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`)
        .join("");
    } catch {
      paramSelect.innerHTML = "";
    }
  }

  // The target value comes from whichever control the domain exposes.
  function currentTarget() {
    const domain = form.elements.domain.value;
    if (domain === "type") return targetTypeSelect.value;
    if (domain === "mounting") return targetMountingSelect.value;
    return targetTextInput.value.trim();
  }

  form.elements.domain.addEventListener("change", syncFields);
  typeSelect.addEventListener("change", loadParams);

  newRuleBtn.addEventListener("click", async () => {
    form.reset();
    error.hidden = true;
    await syncFields();
    dialog.showModal();
  });

  const guardNew = makeGuard();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    guardNew(async () => {
      const scoped = isScoped();
      const payload = {
        domain: form.elements.domain.value,
        alias: form.elements.alias.value.trim(),
        canonical: currentTarget(),
        sort_order: Number(form.elements.sort_order.value) || 0,
        parameter_definition_id:
          scoped && paramSelect.value ? Number(paramSelect.value) : null,
      };
      if (scoped && payload.parameter_definition_id === null) {
        error.textContent = "Pick the parameter this rule applies to.";
        error.hidden = false;
        return;
      }
      if (!payload.canonical) {
        error.textContent = "Pick or enter a target.";
        error.hidden = false;
        return;
      }
      try {
        const resp = await sendRuleWrite(
          "/api/admin/match-rules",
          "POST",
          payload,
        );
        if (resp.ok) {
          dialog.close();
          await loadRules();
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      }
    });
  });
}

rulesTable.on("tableBuilt", loadRules);
loadTypeNames(); // ready the type list for the inline Target editor

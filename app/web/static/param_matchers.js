// Per-parameter matcher management (admin, /types). Opened from a parameter row in
// the "Parameters" dialog, it manages the two kinds of rule a parameter can have, in
// two clearly separated sections:
//   - Value aliases (enum_value): a shop's word for one of the allowed values.
//   - Parameter-name aliases (param_name): a shop's label for the parameter itself.
// Each section lists its rules (alias — and, for a value rule, the target value —
// editable inline), deletes, and RAPID-ADDS without a modal per entry (the chore for
// an enum with many values). Reuses the admin feed (/web/api/match-rules, filtered by
// parameter) and the admin write endpoints. Exposes window.openParamMatchers(param).
// esc/csrfToken/errorMessage come from shared.js.
//
// ONE ROW PER TARGET, as in the admin table: a target's aliases are one
// comma-separated field ("biały, czarny, różowy" → "Kolor"), which is what an enum
// whose values each answer to a handful of spellings actually looks like.
// groupRulesByTarget/aliasListWrites in shared.js do the grouping and turn an edited
// list back into the rule writes that make it true.

(function () {
  const dialog = document.getElementById("param-matchers-dialog");
  if (!dialog) return; // not the types admin page

  const titleEl = document.getElementById("param-matchers-title");
  const errorEl = document.getElementById("param-matchers-error");
  const statusEl = document.getElementById("param-matchers-status");

  const valueSection = document.getElementById("pm-value-section");
  const valueList = document.getElementById("pm-value-list");
  const valueEmpty = document.getElementById("pm-value-empty");
  const valueAlias = document.getElementById("pm-value-alias");
  const valueTarget = document.getElementById("pm-value-target");
  const valueAdd = document.getElementById("pm-value-add");

  const nameList = document.getElementById("pm-name-list");
  const nameEmpty = document.getElementById("pm-name-empty");
  const nameAlias = document.getElementById("pm-name-alias");
  const nameAdd = document.getElementById("pm-name-add");

  const PARAM_NAME = "param_name";
  const ENUM_VALUE = "enum_value";

  let param = null; // the parameter this panel is managing

  function setError(text) {
    errorEl.textContent = text || "";
    errorEl.hidden = !text;
  }

  // Persistent, honest signal of the save model: edits write immediately, so the
  // footer says so — and each successful write flashes "Saved ✓" — so it's always
  // clear the window is safe to close. No Save/Cancel: there is nothing pending.
  const SAVE_HINT = "Changes save automatically.";
  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = kind === "ok" ? "pm-status pm-status-ok" : "pm-status muted";
  }

  function refreshEmpty(list, empty) {
    empty.hidden = list.children.length > 0;
  }

  // A <select> of the parameter's allowed enum values.
  function valueSelect() {
    const select = document.createElement("select");
    select.className = "control pm-target";
    select.innerHTML = (param.enum_values || [])
      .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
      .join("");
    return select;
  }

  // The target's whole vocabulary in one field: what is typed here IS the set, so
  // dropping a word from the list deletes its rule.
  function aliasInput(group) {
    const input = document.createElement("input");
    input.className = "control pm-alias";
    input.value = group.alias;
    input.addEventListener("change", () => {
      const writes = aliasListWrites(group, input.value);
      if (!writes) {
        input.value = group.alias; // an empty list is ✕'s job, not an edit
        setError("A target needs at least one alias — use ✕ to remove it entirely.");
        return;
      }
      runWrites(writes);
    });
    return input;
  }

  function deleteButton(group) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-ghost btn-sm pm-del";
    del.textContent = "✕";
    del.setAttribute("aria-label", `Delete matchers "${group.alias}"`);
    del.addEventListener("click", () =>
      runWrites(group.rules.map((rule) => ({ method: "DELETE", id: rule.id }))),
    );
    return del;
  }

  // Value row: every alias for one value + that value's dropdown + delete.
  function valueRow(group) {
    const li = document.createElement("li");
    li.className = "pm-row";
    const target = valueSelect();
    target.value = group.canonical;
    // The value belongs to the row, not to one of its spellings, so it moves them
    // all — and moving onto a value that already has aliases merges the two rows.
    target.addEventListener("change", () =>
      runWrites(
        group.rules.map((rule) => ({
          method: "PATCH",
          id: rule.id,
          body: { canonical: target.value },
        })),
      ),
    );
    const arrow = document.createElement("span");
    arrow.className = "pm-arrow muted";
    arrow.textContent = "→";
    li.append(aliasInput(group), arrow, target, deleteButton(group));
    return li;
  }

  // Name row: just the aliases (their target is fixed — the parameter's name) + delete.
  function nameRow(group) {
    const li = document.createElement("li");
    li.className = "pm-row";
    li.append(aliasInput(group), deleteButton(group));
    return li;
  }

  // A row stands for several rules now, so a write is a batch and the lists reload
  // after it: a batch that stops at a refusal leaves the earlier writes done, and the
  // panel has to show that rather than whatever was typed. Returns the refusal, if any.
  async function runWrites(writes) {
    setError("");
    setStatus("Saving…");
    const failure = await runMatchRuleWrites(writes);
    await loadLists();
    if (failure) {
      setError(failure);
      setStatus(SAVE_HINT);
    } else {
      setStatus("Saved ✓", "ok");
    }
    return failure;
  }

  // One alias, or a comma-separated run of them onto the same target — the whole
  // point for an enum whose values each answer to a handful of spellings.
  async function addAliases(input, domain, canonical) {
    const aliases = splitAliases(input.value);
    if (!aliases.length) return input.focus();
    const failure = await runWrites(
      aliases.map((alias) => ({
        method: "POST",
        body: {
          domain,
          alias,
          canonical,
          parameter_definition_id: param.id,
          sort_order: 0,
        },
      })),
    );
    // Keep the text on a refusal so it can be fixed; focus never leaves either way,
    // so a run of aliases still goes in one after another.
    if (!failure) input.value = "";
    input.focus();
  }

  // Add-and-stay: the lists reload (a new alias for a value already listed belongs in
  // that row, not in one of its own) but focus never moves, so a run of aliases goes
  // in one after another with no modal.
  const addValue = () => addAliases(valueAlias, ENUM_VALUE, valueTarget.value);
  // A name alias maps onto the parameter's own name.
  const addName = () => addAliases(nameAlias, PARAM_NAME, param.name);

  async function loadLists() {
    valueList.replaceChildren();
    nameList.replaceChildren();
    let rows = [];
    try {
      const payload = await fetch("/web/api/match-rules").then((r) => r.json());
      rows = (payload.data || []).filter(
        (r) => r.parameter_definition_id === param.id,
      );
    } catch {
      setError("Could not load matchers.");
    }
    // The domain is part of the grouping key, so a value group and a name group never
    // merge into each other however their targets read.
    for (const group of groupRulesByTarget(rows)) {
      if (group.domain === ENUM_VALUE) valueList.appendChild(valueRow(group));
      else nameList.appendChild(nameRow(group));
    }
    refreshEmpty(valueList, valueEmpty);
    refreshEmpty(nameList, nameEmpty);
  }

  async function openParamMatchers(p) {
    if (dialog.open) return;
    param = p;
    titleEl.textContent = param.label;
    setError("");
    setStatus(SAVE_HINT);
    valueAlias.value = "";
    nameAlias.value = "";
    // The value section only applies to an enum parameter (one with allowed values).
    const isEnum = (param.enum_values || []).length > 0;
    valueSection.hidden = !isEnum;
    if (isEnum) {
      valueTarget.innerHTML = (param.enum_values || [])
        .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
        .join("");
    }
    await loadLists();
    dialog.showModal();
    (isEnum ? valueAlias : nameAlias).focus();
  }
  window.openParamMatchers = openParamMatchers;

  function addOnEnter(input, add) {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      add();
    });
  }
  valueAdd.addEventListener("click", addValue);
  nameAdd.addEventListener("click", addName);
  addOnEnter(valueAlias, addValue);
  addOnEnter(nameAlias, addName);
})();

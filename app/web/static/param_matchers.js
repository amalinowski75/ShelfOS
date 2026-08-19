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

  async function write(url, method, payload) {
    return fetch(url, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
  }

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

  function aliasInput(rule) {
    const input = document.createElement("input");
    input.className = "control pm-alias";
    input.value = rule.alias;
    input.addEventListener("change", () =>
      patchRule(rule, { alias: input.value.trim() }, input, rule.alias),
    );
    return input;
  }

  function deleteButton(rule, li, list, empty) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-ghost btn-sm pm-del";
    del.textContent = "✕";
    del.setAttribute("aria-label", `Delete matcher "${rule.alias}"`);
    del.addEventListener("click", () => deleteRule(rule, li, list, empty));
    return del;
  }

  // Value rule row: alias + a value dropdown (both editable) + delete.
  function valueRow(rule) {
    const li = document.createElement("li");
    li.className = "pm-row";
    const target = valueSelect();
    target.value = rule.canonical;
    target.addEventListener("change", () =>
      patchRule(rule, { canonical: target.value }, target, rule.canonical),
    );
    const arrow = document.createElement("span");
    arrow.className = "pm-arrow muted";
    arrow.textContent = "→";
    li.append(aliasInput(rule), arrow, target, deleteButton(rule, li, valueList, valueEmpty));
    return li;
  }

  // Name rule row: just the alias (its target is fixed — the parameter's name) + delete.
  function nameRow(rule) {
    const li = document.createElement("li");
    li.className = "pm-row";
    li.append(aliasInput(rule), deleteButton(rule, li, nameList, nameEmpty));
    return li;
  }

  async function patchRule(rule, patch, control, previous) {
    setError("");
    setStatus("Saving…");
    try {
      const resp = await write(`/api/admin/match-rules/${rule.id}`, "PATCH", patch);
      if (resp.ok) {
        Object.assign(rule, patch);
        setStatus("Saved ✓", "ok");
      } else {
        control.value = previous; // revert to the last good value
        setError(await errorMessage(resp));
        setStatus(SAVE_HINT);
      }
    } catch {
      control.value = previous;
      setError("Could not reach the server.");
      setStatus(SAVE_HINT);
    }
  }

  async function deleteRule(rule, li, list, empty) {
    setError("");
    setStatus("Saving…");
    try {
      const resp = await write(`/api/admin/match-rules/${rule.id}`, "DELETE");
      if (resp.ok) {
        li.remove();
        refreshEmpty(list, empty);
        setStatus("Saved ✓", "ok");
      } else {
        setError(await errorMessage(resp));
        setStatus(SAVE_HINT);
      }
    } catch {
      setError("Could not reach the server.");
      setStatus(SAVE_HINT);
    }
  }

  async function createRule(domain, alias, canonical) {
    setError("");
    setStatus("Saving…");
    try {
      const resp = await write("/api/admin/match-rules", "POST", {
        domain,
        alias,
        canonical,
        parameter_definition_id: param.id,
        sort_order: 0,
      });
      if (!resp.ok) {
        setError(await errorMessage(resp)); // e.g. a duplicate alias — keep the text
        setStatus(SAVE_HINT);
        return null;
      }
      setStatus("Saved ✓", "ok");
      return await resp.json();
    } catch {
      setError("Could not reach the server.");
      setStatus(SAVE_HINT);
      return null;
    }
  }

  // Add-and-stay: append the new row, clear the alias, keep focus — a run of aliases
  // goes in one after another with no modal, no reload.
  async function addValue() {
    const alias = valueAlias.value.trim();
    if (!alias) return valueAlias.focus();
    const created = await createRule(ENUM_VALUE, alias, valueTarget.value);
    if (!created) return;
    valueList.appendChild(valueRow(created));
    refreshEmpty(valueList, valueEmpty);
    valueAlias.value = "";
    valueAlias.focus();
  }

  async function addName() {
    const alias = nameAlias.value.trim();
    if (!alias) return nameAlias.focus();
    // A name alias maps onto the parameter's own name.
    const created = await createRule(PARAM_NAME, alias, param.name);
    if (!created) return;
    nameList.appendChild(nameRow(created));
    refreshEmpty(nameList, nameEmpty);
    nameAlias.value = "";
    nameAlias.focus();
  }

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
    for (const rule of rows) {
      if (rule.domain === ENUM_VALUE) valueList.appendChild(valueRow(rule));
      else nameList.appendChild(nameRow(rule));
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

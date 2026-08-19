// Per-parameter matcher management (admin, /types). Opened from a parameter row in
// the "Parameters" dialog, it lists the match rules already attached to that
// parameter, edits alias/target inline, deletes, and — the point — RAPID-ADDS aliases
// without a modal per value (the chore for an enum with many allowed values). Reuses
// the admin feed (/web/api/match-rules, filtered by parameter) and the admin write
// endpoints. Exposes window.openParamMatchers(param, typeId). esc/csrfToken/
// errorMessage come from shared.js.

(function () {
  const dialog = document.getElementById("param-matchers-dialog");
  if (!dialog) return; // not the types admin page

  const titleEl = document.getElementById("param-matchers-title");
  const listEl = document.getElementById("param-matchers-list");
  const emptyEl = document.getElementById("param-matchers-empty");
  const errorEl = document.getElementById("param-matchers-error");
  const addAlias = document.getElementById("pm-add-alias");
  const addTarget = document.getElementById("pm-add-target");
  const addBtn = document.getElementById("pm-add-btn");

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

  function refreshEmpty() {
    emptyEl.hidden = listEl.children.length > 0;
  }

  // One rule row: an editable alias, its target, and a delete button. The target is a
  // value dropdown for an enum_value rule (edit in place) and static text for a
  // param_name rule (its canonical must equal the parameter's name, so it's fixed).
  function buildRow(rule) {
    const li = document.createElement("li");
    li.className = "pm-row";
    li.dataset.ruleId = rule.id;

    const alias = document.createElement("input");
    alias.className = "control pm-alias";
    alias.value = rule.alias;
    alias.addEventListener("change", () =>
      patchRule(rule, { alias: alias.value.trim() }, alias, rule.alias),
    );

    const arrow = document.createElement("span");
    arrow.className = "pm-arrow muted";
    arrow.textContent = "→";

    let target;
    if (rule.domain === ENUM_VALUE) {
      target = valueSelect();
      target.value = rule.canonical;
      target.addEventListener("change", () =>
        patchRule(rule, { canonical: target.value }, target, rule.canonical),
      );
    } else {
      target = document.createElement("span");
      target.className = "pm-target-static muted";
      target.textContent = "(parameter name)";
    }
    target.classList.add("pm-target");

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-ghost btn-sm pm-del";
    del.textContent = "✕";
    del.setAttribute("aria-label", `Delete matcher "${rule.alias}"`);
    del.addEventListener("click", () => deleteRule(rule, li));

    li.append(alias, arrow, target, del);
    return li;
  }

  // A <select> of the parameter's allowed enum values.
  function valueSelect() {
    const select = document.createElement("select");
    select.className = "control";
    select.innerHTML = (param.enum_values || [])
      .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
      .join("");
    return select;
  }

  async function patchRule(rule, patch, control, previous) {
    setError("");
    try {
      const resp = await write(`/api/admin/match-rules/${rule.id}`, "PATCH", patch);
      if (resp.ok) {
        Object.assign(rule, patch);
      } else {
        control.value = previous; // revert the field to the last good value
        setError(await errorMessage(resp));
      }
    } catch {
      control.value = previous;
      setError("Could not reach the server.");
    }
  }

  async function deleteRule(rule, li) {
    setError("");
    try {
      const resp = await write(`/api/admin/match-rules/${rule.id}`, "DELETE");
      if (resp.ok) {
        li.remove();
        refreshEmpty();
      } else {
        setError(await errorMessage(resp));
      }
    } catch {
      setError("Could not reach the server.");
    }
  }

  // The quick-add target: each enum value (an enum_value rule) plus an "as the
  // parameter name" option (a param_name rule — canonical must be the param's name).
  // A non-enum parameter has no values, so only the name option remains and the
  // select is hidden — the alias alone makes the rule.
  function buildAddTarget() {
    const options = (param.enum_values || []).map(
      (v) => `<option data-domain="${ENUM_VALUE}" value="${esc(v)}">${esc(v)}</option>`,
    );
    options.push(
      `<option data-domain="${PARAM_NAME}" value="${esc(param.name)}">` +
        `↳ as parameter name</option>`,
    );
    addTarget.innerHTML = options.join("");
    addTarget.selectedIndex = 0;
    addTarget.hidden = (param.enum_values || []).length === 0;
  }

  async function addRule() {
    const alias = addAlias.value.trim();
    if (!alias) {
      addAlias.focus();
      return;
    }
    const option = addTarget.options[addTarget.selectedIndex];
    setError("");
    let created;
    try {
      const resp = await write("/api/admin/match-rules", "POST", {
        domain: option.dataset.domain,
        alias,
        canonical: option.value,
        parameter_definition_id: param.id,
        sort_order: 0,
      });
      if (!resp.ok) {
        setError(await errorMessage(resp)); // e.g. a duplicate alias — keep the text
        return;
      }
      created = await resp.json();
    } catch {
      setError("Could not reach the server.");
      return;
    }
    // Append and clear for the next one — no modal, no reload, focus stays put so a
    // run of aliases goes in one after another.
    listEl.appendChild(buildRow(created));
    refreshEmpty();
    addAlias.value = "";
    addAlias.focus();
  }

  async function loadList() {
    listEl.replaceChildren();
    let rows = [];
    try {
      const payload = await fetch("/web/api/match-rules").then((r) => r.json());
      rows = (payload.data || []).filter(
        (r) => r.parameter_definition_id === param.id,
      );
    } catch {
      setError("Could not load matchers.");
    }
    for (const rule of rows) listEl.appendChild(buildRow(rule));
    refreshEmpty();
  }

  async function openParamMatchers(p, _typeId) {
    if (dialog.open) return;
    param = p;
    titleEl.textContent = param.label;
    setError("");
    addAlias.value = "";
    buildAddTarget();
    await loadList();
    dialog.showModal();
    addAlias.focus();
  }
  window.openParamMatchers = openParamMatchers;

  addBtn.addEventListener("click", addRule);
  // Enter in the alias field adds too (rapid-fire), never submitting a form.
  addAlias.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addRule();
  });
})();

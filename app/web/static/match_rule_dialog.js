// The "New match rule" create dialog, extracted from match_rules.js so it works
// wherever it's included — the match-rules admin table, but also the type builder and
// a type's parameter list ("everything within reach": create a matcher without
// leaving to the admin page). Exposes `window.openMatcherDialog(onCreated, prefill)`;
// `prefill` = { domain?, typeId?, parameterDefinitionId? } pre-scopes the rule so the
// caller can point it straight at a parameter. Gated on the dialog being present.
// esc/csrfToken/errorMessage come from shared.js.

(function () {
  const dialog = document.getElementById("rule-new-dialog");
  if (!dialog) return; // not an admin page / dialog not included here

  const form = document.getElementById("rule-new-form");
  const error = document.getElementById("rule-new-error");
  const typeField = document.getElementById("rule-scope-type");
  const paramField = document.getElementById("rule-scope-param");
  const typeSelect = form.elements.type;
  const paramSelect = form.elements.parameter;
  // The four possible "Target" controls; only the one matching the domain shows.
  const targetTypeSelect = form.elements.canonical_type; // type rule → a type name
  const targetMountingSelect = form.elements.canonical_mounting; // mounting → enum
  const targetEnumSelect = form.elements.canonical_enum; // enum_value → allowed value
  const targetTextInput = form.elements.canonical_text; // param_name → free text

  // The two domains that attach a rule to a single parameter definition.
  const SCOPED_DOMAINS = new Set(["param_name", "enum_value"]);
  // The selected type's parameter definitions (with data_type + enum_values), cached
  // so the parameter picker and the enum-target dropdown can be built without refetch.
  let dialogParams = [];
  // Fired after a successful create; consumed once so a stale callback can't re-run.
  let pendingOnCreated = null;

  function isScoped() {
    return SCOPED_DOMAINS.has(form.elements.domain.value);
  }

  // Reflect the chosen domain: show its scope pickers (param/enum only) and the one
  // Target control that fits — an existing-type list, the mounting enum, the chosen
  // parameter's allowed values, or free text — so a bad target can't be entered.
  async function syncFields() {
    const domain = form.elements.domain.value;
    const scoped = isScoped();
    typeField.hidden = !scoped;
    paramField.hidden = !scoped;
    targetTypeSelect.hidden = domain !== "type";
    targetMountingSelect.hidden = domain !== "mounting";
    targetEnumSelect.hidden = domain !== "enum_value";
    targetTextInput.hidden = domain !== "param_name";
    // Refetch the type list (and, for a scoped domain, its parameters) every time this
    // runs — on dialog reopen `form.reset()` snaps Type back to its first option WITHOUT
    // firing `change`, so without a reload the parameter picker would still hold the
    // previous type's params and the rule would bind to the wrong parameter. Reloading
    // also surfaces a type added since the page loaded.
    if (scoped || domain === "type") {
      await loadTypes(); // ends by calling loadParams -> populateParams for scoped
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
      await loadParams();
    } catch {
      error.textContent = "Could not load types.";
      error.hidden = false;
    }
  }

  async function loadParams() {
    const typeId = typeSelect.value;
    try {
      dialogParams = typeId
        ? await fetch(`/api/types/${typeId}/parameters`).then((r) => r.json())
        : [];
    } catch {
      dialogParams = [];
    }
    populateParams();
  }

  // Fill the parameter picker; an enum_value rule only makes sense for an enum
  // parameter, so those are all it offers.
  function populateParams() {
    const enumOnly = form.elements.domain.value === "enum_value";
    const choices = enumOnly
      ? dialogParams.filter((p) => p.data_type === "enum")
      : dialogParams;
    paramSelect.innerHTML = choices
      .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`)
      .join("");
    populateEnumTarget();
  }

  // For an enum_value rule, the Target is the chosen parameter's allowed values —
  // the tokens defined when the type was built — so it can't be free-typed wrong.
  function populateEnumTarget() {
    if (form.elements.domain.value !== "enum_value") return;
    const chosen = dialogParams.find((p) => String(p.id) === paramSelect.value);
    targetEnumSelect.innerHTML = (chosen?.enum_values || [])
      .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
      .join("");
  }

  // The target value comes from whichever control the domain exposes.
  function currentTarget() {
    const domain = form.elements.domain.value;
    if (domain === "type") return targetTypeSelect.value;
    if (domain === "mounting") return targetMountingSelect.value;
    if (domain === "enum_value") return targetEnumSelect.value;
    return targetTextInput.value.trim(); // param_name
  }

  form.elements.domain.addEventListener("change", syncFields);
  typeSelect.addEventListener("change", loadParams);
  paramSelect.addEventListener("change", populateEnumTarget);

  // Open the dialog, optionally pre-scoped to a parameter. `prefill` seeds the domain,
  // then the owning type and its parameter, so a caller standing on a parameter can
  // create its matcher without re-picking anything. On success `onCreated(created)`
  // fires (the admin table passes its reload; other callers can toast).
  async function openMatcherDialog(onCreated, prefill) {
    if (dialog.open) return; // showModal() on an open dialog throws
    pendingOnCreated = onCreated || null;
    form.reset();
    error.hidden = true;
    const pf = prefill || {};
    if (pf.domain) form.elements.domain.value = pf.domain;
    await syncFields(); // loads the type list (+ first type's params) for scoped/type
    if (pf.typeId != null && isScoped()) {
      typeSelect.value = String(pf.typeId);
      await loadParams();
      if (pf.parameterDefinitionId != null) {
        paramSelect.value = String(pf.parameterDefinitionId);
        populateEnumTarget();
      }
      // A param_name rule maps a label onto a parameter's own name — default the
      // target to it (the common case), still editable.
      if (form.elements.domain.value === "param_name") {
        const chosen = dialogParams.find((p) => String(p.id) === paramSelect.value);
        if (chosen) targetTextInput.value = chosen.name;
      }
    }
    dialog.showModal();
  }
  window.openMatcherDialog = openMatcherDialog;

  async function sendRuleWrite(url, method, payload) {
    return fetch(url, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
  }

  // Ignore a re-entrant submit while a write is in flight (stops a double-click
  // sending a duplicate request).
  let inFlight = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (inFlight) return;
    inFlight = true;
    (async () => {
      try {
        const scoped = isScoped();
        const base = {
          domain: form.elements.domain.value,
          canonical: currentTarget(),
          sort_order: Number(form.elements.sort_order.value) || 0,
          parameter_definition_id:
            scoped && paramSelect.value ? Number(paramSelect.value) : null,
        };
        if (scoped && base.parameter_definition_id === null) {
          error.textContent = "Pick the parameter this rule applies to.";
          error.hidden = false;
          return;
        }
        if (!base.canonical) {
          error.textContent = "Pick or enter a target.";
          error.hidden = false;
          return;
        }
        // One field, a whole vocabulary: "biały, czarny, różowy" onto one target is a
        // rule each, so a set of synonyms goes in without reopening the dialog per
        // word. A single alias is just a list of one.
        const aliases = splitAliases(form.elements.alias.value);
        if (!aliases.length) {
          error.textContent = "Enter at least one alias.";
          error.hidden = false;
          return;
        }
        let created = null;
        let landed = 0;
        let refused = null;
        for (const alias of aliases) {
          const resp = await sendRuleWrite("/api/admin/match-rules", "POST", {
            ...base,
            alias,
          });
          if (!resp.ok) {
            refused = await errorMessage(resp);
            break;
          }
          created = await resp.json().catch(() => null);
          landed += 1;
        }
        if (refused) {
          // Stopping partway leaves the earlier aliases created, so leave only what
          // still has to go in — resubmitting retries exactly the remainder, and the
          // list behind is refreshed so the ones that did land are visible.
          error.textContent = refused;
          error.hidden = false;
          form.elements.alias.value = aliases.slice(landed).join(ALIAS_SEPARATOR);
          if (landed && pendingOnCreated) await pendingOnCreated(created);
          return;
        }
        dialog.close();
        const callback = pendingOnCreated;
        pendingOnCreated = null; // consume, so it can't fire against a later open
        if (callback) await callback(created);
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      } finally {
        inFlight = false;
      }
    })();
  });
})();

// Shared "New component" dialog (spec §16.5). Loaded on every authenticated
// page; active only where the _component_dialog.html partial is present.
// Exposes `openComponentDialog(onCreated, prefill)` — `onCreated` receives the
// created component so each caller can react (filter the table, select it in a
// picker…); the optional `prefill` ({category, value, mpn, manufacturer}) seeds
// the fields from a BOM line. Uses shared.js helpers (csrfToken, errorMessage).

(function () {
  const dialog = document.getElementById("component-dialog");
  if (!dialog) return; // page does not include the dialog

  const form = document.getElementById("component-form");
  const typeSelect = document.getElementById("component-type");
  const paramsBox = document.getElementById("component-params");
  const paramsHint = document.getElementById("component-params-hint");
  const errorEl = document.getElementById("component-error");

  // Monotonic id so overlapping type-select changes can't render stale fields.
  let paramsRequestId = 0;
  let onCreated = null;
  // The effective parameter definitions currently rendered (for prefill lookups).
  let currentDefinitions = [];

  // Build a value input for one effective parameter definition, keyed by its id
  // and data type so the payload can be assembled without another lookup.
  function buildParamField(definition) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = definition.unit
      ? `${definition.label} (${definition.unit})`
      : definition.label;
    field.appendChild(label);

    let input;
    if (definition.data_type === "enum") {
      input = document.createElement("select");
      input.className = "control";
      input.appendChild(new Option("—", ""));
      for (const value of definition.enum_values) {
        input.appendChild(new Option(value, value));
      }
    } else if (definition.data_type === "bool") {
      input = document.createElement("select");
      input.className = "control";
      input.appendChild(new Option("—", ""));
      input.appendChild(new Option("yes", "true"));
      input.appendChild(new Option("no", "false"));
    } else {
      input = document.createElement("input");
      input.className = "control";
      input.type = "text";
      if (definition.data_type === "number") input.placeholder = "e.g. 4k7";
    }
    input.dataset.definitionId = definition.id;
    input.dataset.dataType = definition.data_type;
    field.appendChild(input);
    return field;
  }

  async function loadParams(typeId) {
    const requestId = ++paramsRequestId;
    paramsBox.replaceChildren();
    currentDefinitions = [];
    if (!typeId) {
      paramsHint.textContent = "Select a type to enter its parameters.";
      paramsHint.hidden = false;
      return;
    }
    // Show a placeholder synchronously so a stale hint from the previous type
    // is never visible while the new type's parameters are loading.
    paramsHint.textContent = "Loading…";
    paramsHint.hidden = false;
    let definitions;
    try {
      const resp = await fetch(`/api/types/${typeId}/parameters`);
      if (!resp.ok) throw new Error();
      definitions = await resp.json();
    } catch {
      if (requestId !== paramsRequestId) return; // superseded by a newer pick
      paramsHint.textContent = "Could not load this type's parameters.";
      paramsHint.hidden = false;
      return;
    }
    if (requestId !== paramsRequestId) return; // a newer selection is in flight
    if (!definitions.length) {
      paramsHint.textContent = "This type has no parameters.";
      paramsHint.hidden = false;
      return;
    }
    paramsHint.hidden = true;
    currentDefinitions = definitions;
    for (const definition of definitions) {
      paramsBox.appendChild(buildParamField(definition));
    }
  }

  // Collect only the filled fields; a bool select maps to a real boolean, and a
  // number is sent as its raw string so the server parses the engineering value.
  function collectParameters() {
    const params = [];
    for (const input of paramsBox.querySelectorAll("[data-definition-id]")) {
      const raw = input.value.trim();
      if (!raw) continue;
      const value = input.dataset.dataType === "bool" ? raw === "true" : raw;
      params.push({
        parameter_definition_id: Number(input.dataset.definitionId),
        value,
      });
    }
    return params;
  }

  typeSelect.addEventListener("change", (event) => loadParams(event.target.value));

  // Ignore a re-entrant submit while a create is in flight, so a fast
  // double-click can't POST two components.
  let submitting = false;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    try {
      const value = (name) => form.querySelector(`[name="${name}"]`).value.trim();
      const body = JSON.stringify({
        // Guard the empty selection (Number("") is 0, not null); the server then
        // rejects a missing type cleanly rather than looking up type 0.
        type_id: typeSelect.value ? Number(typeSelect.value) : null,
        manufacturer: value("manufacturer") || null,
        mpn: value("mpn") || null,
        package: value("package") || null,
        mounting_type: form.querySelector('[name="mounting_type"]').value,
        notes: value("notes") || null,
        parameters: collectParameters(),
      });

      errorEl.hidden = true;
      let created;
      try {
        const resp = await fetch("/api/components", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body,
        });
        if (!resp.ok) {
          errorEl.textContent = await errorMessage(resp);
          errorEl.hidden = false;
          return;
        }
        created = await resp.json();
      } catch {
        errorEl.textContent = "Could not reach the server. Please try again.";
        errorEl.hidden = false;
        return;
      }
      dialog.close();
      // A caller's DOM update must not become an unhandled rejection: the
      // component is already persisted (mirrors location_dialog.js).
      if (onCreated) {
        try {
          onCreated(created);
        } catch {
          /* swallow — the component was created; only the caller's hook failed */
        }
      }
    } finally {
      submitting = false;
    }
  });

  // Select the type whose name matches `category` (case-insensitive); null if none.
  function matchTypeByName(category) {
    if (!category) return null;
    const target = String(category).trim().toLowerCase();
    const option = [...typeSelect.options].find(
      (o) => o.value && o.textContent.trim().toLowerCase() === target,
    );
    return option ? option.value : null;
  }

  // The type's "value" parameter, chosen exactly like the server's bom_service
  // (`_value_parameter`): the NUMBER definition with the lowest (sort_order, id)
  // across the WHOLE effective set — NOT DOM order, which is ancestor-first and
  // would pick an inherited parameter over a lower-order own one.
  function valueParamId() {
    const numbers = currentDefinitions.filter((d) => d.data_type === "number");
    if (!numbers.length) return null;
    return numbers.reduce((best, d) =>
      d.sort_order < best.sort_order ||
      (d.sort_order === best.sort_order && d.id < best.id)
        ? d
        : best,
    ).id;
  }

  // Put a BOM line's value into that value parameter. The raw value may carry a
  // tolerance/voltage suffix ("10k 1%", "10uF/50V"); keep only the token the
  // number parser accepts, mirroring the server's clean_value.
  function setValueParam(rawValue) {
    const id = valueParamId();
    if (id == null) return;
    const input = paramsBox.querySelector(`input[data-definition-id="${id}"]`);
    if (input) input.value = String(rawValue).split(/[\s/]/)[0];
  }

  // Pre-fill from a BOM line: { category, value, mpn, manufacturer }. Runs async
  // (loading the matched type's parameters); fired after the dialog is shown.
  async function applyPrefill(prefill) {
    if (!prefill) {
      loadParams(""); // clears the fields and shows the hint
      return;
    }
    if (prefill.mpn) form.querySelector('[name="mpn"]').value = prefill.mpn;
    if (prefill.manufacturer) {
      form.querySelector('[name="manufacturer"]').value = prefill.manufacturer;
    }
    const typeId = matchTypeByName(prefill.category);
    if (!typeId) {
      loadParams(""); // no matching type — let the user pick one
      return;
    }
    typeSelect.value = typeId;
    await loadParams(typeId); // render the type's parameter fields
    // Only fill the value if the user hasn't changed the type while we loaded.
    if (prefill.value && typeSelect.value === typeId) setValueParam(prefill.value);
  }

  // Open the dialog; `callback(created)` runs after a successful create. An
  // optional `prefill` seeds the fields from a BOM line.
  window.openComponentDialog = function (callback, prefill) {
    onCreated = callback || null;
    form.reset();
    errorEl.hidden = true;
    dialog.showModal(); // open synchronously; fields fill in a tick later
    applyPrefill(prefill);
  };
})();

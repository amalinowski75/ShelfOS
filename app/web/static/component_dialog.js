// Shared "New component" dialog (spec §16.5). Loaded on every authenticated
// page; active only where the _component_dialog.html partial is present.
// Exposes `openComponentDialog(onCreated)` — `onCreated` receives the created
// component so each caller can react (filter the table, select it in a picker…).
// Uses shared.js helpers (csrfToken, errorMessage).

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

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
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
    if (onCreated) onCreated(created);
  });

  // Open the dialog; `callback(created)` runs after a successful create.
  window.openComponentDialog = function (callback) {
    onCreated = callback || null;
    form.reset();
    errorEl.hidden = true;
    loadParams(""); // clears the fields and shows the hint
    dialog.showModal();
  };
})();

// Admin-only component edit (§12). The "Edit" button opens a dialog pre-filled with
// the component's current scalar fields (rendered server-side) and its parameter
// values (built here from the live values); Save PATCHes /api/components/{id} and
// reloads. Type and MPN are immutable and shown read-only in the dialog.
// esc/csrfToken/errorMessage come from shared.js.

(function () {
  const dialog = document.getElementById("component-edit-dialog");
  const editBtn = document.getElementById("component-edit-btn");
  if (!dialog || !editBtn) return; // non-admin, or not the detail page

  const form = document.getElementById("component-edit-form");
  const paramsBox = document.getElementById("component-edit-params");
  const error = document.getElementById("component-edit-error");
  const componentId = dialog.dataset.componentId;
  const typeId = dialog.dataset.typeId;

  let definitions = [];

  // Build one pre-filled value input for a parameter definition. `current` is the
  // stored value (number/string/bool) or undefined/null when unset.
  function buildParamField(def, current) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = def.unit ? `${def.label} (${def.unit})` : def.label;
    field.appendChild(label);

    let input;
    if (def.data_type === "enum" || def.data_type === "bool") {
      input = document.createElement("select");
      const options =
        def.data_type === "bool"
          ? [["", "—"], ["true", "yes"], ["false", "no"]]
          : [["", "—"], ...def.enum_values.map((v) => [v, v])];
      for (const [value, text] of options) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = text;
        input.appendChild(opt);
      }
      if (def.data_type === "bool") {
        input.value = current === true ? "true" : current === false ? "false" : "";
      } else {
        input.value = current == null ? "" : String(current);
      }
    } else {
      input = document.createElement("input");
      if (def.data_type === "number") input.placeholder = "e.g. 4k7";
      input.value = current == null ? "" : String(current);
    }
    input.className = "control";
    input.dataset.definitionId = def.id;
    input.dataset.dataType = def.data_type;
    field.appendChild(input);
    return field;
  }

  // (Re)build the parameter fields from the live definitions + values, so every
  // open starts from the current server state.
  async function loadParams() {
    paramsBox.replaceChildren();
    const [defsResp, valsResp] = await Promise.all([
      fetch(`/api/types/${typeId}/parameters`),
      fetch(`/api/components/${componentId}/parameters`),
    ]);
    if (!defsResp.ok || !valsResp.ok) throw new Error("could not load parameters");
    definitions = await defsResp.json();
    const rows = await valsResp.json();
    const current = {};
    for (const row of rows) {
      // Exactly one column is populated; ?? keeps a real 0 / false (not nullish).
      current[row.parameter_definition_id] =
        row.value_num ?? row.value_text ?? row.value_bool;
    }
    for (const def of definitions) {
      paramsBox.appendChild(buildParamField(def, current[def.id]));
    }
  }

  // Read every parameter field back into the PATCH shape. A blank value is sent as
  // null, which clears (deletes) that parameter.
  function collectParameters() {
    return [...paramsBox.querySelectorAll("[data-definition-id]")].map((input) => {
      const id = Number(input.dataset.definitionId);
      const type = input.dataset.dataType;
      const raw = input.value;
      let value;
      if (type === "bool") value = raw === "" ? null : raw === "true";
      else value = raw.trim() === "" ? null : raw;
      return { parameter_definition_id: id, value };
    });
  }

  editBtn.addEventListener("click", async () => {
    error.hidden = true;
    form.reset(); // restore the server-rendered scalar fields
    try {
      await loadParams();
    } catch {
      paramsBox.replaceChildren();
      error.textContent = "Could not load the current parameters — try again.";
      error.hidden = false;
    }
    dialog.showModal();
  });

  let submitting = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    (async () => {
      try {
        const value = (name) => form.querySelector(`[name="${name}"]`).value.trim();
        const resp = await fetch(`/api/components/${componentId}`, {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            manufacturer: value("manufacturer") || null,
            package: value("package") || null,
            mounting_type: form.querySelector('[name="mounting_type"]').value,
            notes: value("notes") || null,
            parameters: collectParameters(),
          }),
        });
        if (resp.ok) {
          window.location.reload(); // show the saved state
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server. Please try again.";
        error.hidden = false;
      } finally {
        submitting = false;
      }
    })();
  });
})();

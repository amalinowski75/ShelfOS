// The "New component type" dialog (spec §13), extracted so it works wherever it's
// included — the components list AND the invoice line's "New component" flow. It
// exposes `window.openTypeDialog(onCreated)`; the caller decides what happens with
// the created type (become the active filter, or get selected in the component
// dialog). Gated on the dialog being present, not on any page-specific trigger.
// esc/csrfToken/errorMessage come from shared.js.

(function () {
  const typeDialog = document.getElementById("type-dialog");
  if (!typeDialog) return; // not a writer / not a page with the type builder

  const typeForm = document.getElementById("type-form");
  const paramsBox = document.getElementById("params");
  const paramsEmpty = document.getElementById("params-empty");
  const rowTemplate = document.getElementById("param-row-template");

  const refreshEmptyHint = () => {
    paramsEmpty.hidden = paramsBox.children.length > 0;
  };

  function addParamRow() {
    const row = rowTemplate.content.firstElementChild.cloneNode(true);
    const dataType = row.querySelector('[name="p-data-type"]');
    const enumField = row.querySelector(".param-enum");
    // Allowed values only make sense for an enum parameter. Sync from the current
    // value too, not just on change, so the field is correct even if "enum" is ever
    // the default-selected data type.
    const syncEnumField = () => {
      enumField.hidden = dataType.value !== "enum";
    };
    dataType.addEventListener("change", syncEnumField);
    syncEnumField();
    row.querySelector(".param-remove").addEventListener("click", () => {
      row.remove();
      refreshEmptyHint();
    });
    paramsBox.appendChild(row);
    refreshEmptyHint();
    row.querySelector('[name="p-name"]').focus();
  }

  function resetTypeForm() {
    typeForm.reset();
    paramsBox.replaceChildren();
    document.getElementById("type-error").hidden = true;
    refreshEmptyHint();
    loadInheritedParams("");
  }

  // Monotonic id so overlapping parent-select changes can't render a stale response:
  // only the newest request is allowed to touch the DOM.
  let inheritedRequestId = 0;

  // Show the effective parameter set of the chosen parent so the user can see what
  // this type will already inherit and avoid redefining it (spec §13, D3).
  async function loadInheritedParams(parentId) {
    const requestId = ++inheritedRequestId;
    const hint = document.getElementById("inherited-hint");
    const list = document.getElementById("inherited-list");
    list.replaceChildren();
    if (!parentId) {
      hint.textContent =
        "Select a parent type to see the parameters this type will inherit.";
      hint.hidden = false;
      return;
    }
    let params;
    try {
      const resp = await fetch(`/api/types/${parentId}/parameters`);
      if (!resp.ok) throw new Error();
      params = await resp.json();
    } catch {
      if (requestId !== inheritedRequestId) return; // superseded by a newer pick
      hint.textContent = "Could not load inherited parameters.";
      hint.hidden = false;
      return;
    }
    if (requestId !== inheritedRequestId) return; // a newer selection is in flight
    if (!params.length) {
      hint.textContent = "This parent type defines no parameters.";
      hint.hidden = false;
      return;
    }
    hint.hidden = true;
    for (const p of params) {
      const meta = [p.label, p.data_type];
      if (p.unit) meta.push(p.unit);
      let metaText = meta.map(esc).join(" · ");
      if (p.data_type === "enum" && p.enum_values?.length) {
        metaText += ` (${p.enum_values.map(esc).join(", ")})`;
      }
      const li = document.createElement("li");
      li.className = "inherited-item";
      li.innerHTML =
        `<span class="ip-name">${esc(p.name)}</span>` +
        `<span class="ip-meta">${metaText}</span>`;
      list.appendChild(li);
    }
  }

  function collectParameters() {
    return [...paramsBox.querySelectorAll(".param-row")].map((row, index) => {
      const get = (name) => row.querySelector(`[name="${name}"]`);
      const dataType = get("p-data-type").value;
      const param = {
        name: get("p-name").value.trim(),
        label: get("p-label").value.trim(),
        data_type: dataType,
        unit: get("p-unit").value.trim() || null,
        is_table_column: get("p-table").checked,
        is_filterable: get("p-filter").checked,
        sort_order: index,
      };
      // Only enum parameters carry allowed values; the API rejects them on other
      // data types, so leave the key off entirely otherwise.
      if (dataType === "enum") {
        param.enum_values = get("p-enum")
          .value.split(",")
          .map((token) => token.trim())
          .filter(Boolean);
      }
      return param;
    });
  }

  // Add `type` to `select` if absent; optionally select it.
  function upsertTypeOption(select, type, selected) {
    let option = [...select.options].find((o) => o.value === String(type.id));
    if (!option) {
      option = document.createElement("option");
      option.value = type.id;
      option.textContent = type.name;
      select.appendChild(option);
    }
    if (selected) select.value = String(type.id);
  }

  // Open the dialog; on a successful create `onCreated(createdType)` fires. Mirrors
  // openComponentDialog. Consumed after firing so a stale callback can't run later.
  let onTypeCreated = null;
  function openTypeDialog(onCreated) {
    if (typeDialog.open) return; // showModal() on an open dialog throws
    onTypeCreated = onCreated || null;
    resetTypeForm();
    typeDialog.showModal();
  }
  window.openTypeDialog = openTypeDialog;

  document.getElementById("add-param").addEventListener("click", addParamRow);
  typeForm
    .querySelector('[name="parent-id"]')
    .addEventListener("change", (event) => loadInheritedParams(event.target.value));

  let submitting = false;
  typeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    (async () => {
      try {
        const parentValue = typeForm.querySelector('[name="parent-id"]').value;
        const resp = await fetch("/api/types", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body: JSON.stringify({
            name: typeForm.querySelector('[name="type-name"]').value.trim(),
            parent_id: parentValue ? Number(parentValue) : null,
            parameters: collectParameters(),
          }),
        });
        if (resp.ok) {
          const created = await resp.json();
          // Always a valid parent for the next type created this session; the
          // caller's onCreated handles the rest (filter+reload, or select in the
          // component dialog).
          upsertTypeOption(
            typeForm.querySelector('[name="parent-id"]'), created, false,
          );
          typeDialog.close();
          const callback = onTypeCreated;
          onTypeCreated = null; // consume it, so it can't fire against a later open
          if (callback) await callback(created);
        } else {
          const el = document.getElementById("type-error");
          el.textContent = await errorMessage(resp);
          el.hidden = false;
        }
      } catch {
        const el = document.getElementById("type-error");
        el.textContent = "Could not reach the server. Please try again.";
        el.hidden = false;
      } finally {
        submitting = false;
      }
    })();
  });
})();

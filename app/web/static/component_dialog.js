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
  // A datasheet URL from a shop import, attached to the component after it's created.
  let pendingDatasheetUrl = null;
  // Bumped on every open so a slow shop-lookup can't prefill a reopened dialog.
  let openToken = 0;

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
      // Attach an imported datasheet to the new component (best-effort: a failed
      // fetch mustn't undo the create). Reuses the SSRF-guarded from-URL endpoint.
      // Some shops sit behind a bot challenge (TME's document host answers a
      // server-side GET with a Cloudflare 403), so failure here is expected rather
      // than exceptional — but it is reported, never swallowed: a silently missing
      // datasheet is indistinguishable from a part that simply hasn't got one.
      let datasheetFailed = false;
      if (pendingDatasheetUrl && created && created.id) {
        try {
          const attached = await fetch("/api/attachments/from-url", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
            body: JSON.stringify({
              entity_type: "component",
              entity_id: created.id,
              url: pendingDatasheetUrl,
              kind: "datasheet",
            }),
          });
          datasheetFailed = !attached.ok; // a non-2xx does NOT throw
        } catch {
          datasheetFailed = true; // the component exists; only the datasheet is lost
        }
      }
      dialog.close();
      if (datasheetFailed) {
        showToast(
          "Component created, but its datasheet could not be downloaded — " +
            "the shop blocks automated downloads. Attach it by hand if you need it.",
        );
      }
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

  // Normalise a parameter name for matching (drop case and punctuation/spaces).
  function normalizeName(name) {
    return String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  // Turn a shop value like "10 kOhms" into "10k" for a NUMBER field (µ→u, K→k).
  // Applied ONLY to number fields, so a matched text value (e.g. a marking code
  // that starts with digits) isn't silently truncated.
  const _MULTIPLIERS = new Set(["p", "n", "u", "k", "M", "G", "m"]);
  function cleanNumberValue(raw) {
    const text = String(raw ?? "").trim();
    const m = text.match(/^[±\s]*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zµΩ]*)/);
    if (!m) return text;
    const first = { µ: "u", K: "k" }[m[2][0]] || m[2][0] || "";
    return m[1] + (_MULTIPLIERS.has(first) ? first : "");
  }

  // Some shops (Mouser) return only logistics in their attributes — the real specs
  // sit in the free-text description ("Thick Film Resistors - SMD 1.2 kOhms 50 V
  // 100 mW 1 % 0402"). Rather than a parser per category, scan the description with
  // the TYPE'S OWN parameter units: a resistor's Ω/W/% params pick up their values
  // and the stray "50 V" is ignored (it has no volt parameter), while a capacitor's
  // F/V params pick up theirs. Units are keyed lowercase.
  const _UNIT_PATTERNS = {
    ohm: "(?:[Oo]hms?|Ω)",
    ω: "(?:[Oo]hms?|Ω)",
    "%": "%",
    w: "W",
    v: "V",
    f: "F",
    a: "A",
    h: "H",
    hz: "Hz",
  };

  // A number, possibly a fraction: resistor power is quoted as "1/16W", and taking
  // just the digits before the unit would read that as 16 W.
  const _NUMBER = "\\d+(?:\\.\\d+)?(?:/\\d+(?:\\.\\d+)?)?";

  function findValueForUnit(text, unit) {
    const pattern = _UNIT_PATTERNS[String(unit || "").trim().toLowerCase()];
    if (!pattern) return null;
    // The multiplier stays case-sensitive (m = milli vs M = mega); the unit pattern
    // is written case-tolerantly. The lookahead stops "W" matching inside a word.
    const match = text.match(
      new RegExp(`(${_NUMBER})\\s*([pnµukKMGm])?\\s*${pattern}(?![A-Za-z])`),
    );
    if (!match) return null;
    let number = match[1];
    if (number.includes("/")) {
      const [top, bottom] = number.split("/").map(Number);
      if (!bottom) return null;
      number = String(top / bottom); // 1/16 W → 0.0625
    }
    const mult = { µ: "u", K: "k" }[match[2]] || match[2] || "";
    return number + (_MULTIPLIERS.has(mult) ? mult : "");
  }

  // Some descriptions give the primary value with no unit at all ("Thin Film
  // Resistors - SMD TNPW-0402 1.2K 0.1% T-9"). Fill the type's VALUE parameter
  // (lowest (sort_order, id) NUMBER — the same one the BOM report uses) from the
  // first bare engineering token. Deliberately limited to that one field: a bare
  // number elsewhere is usually a package code or a temperature coefficient. A
  // multiplier is required, so "0402" and "100 PPM" can't be mistaken for a value.
  const _BARE_ENGINEERING = /(?:^|[\s\-])(\d+(?:\.\d+)?)([pnµukKMG])(?![A-Za-z0-9])/;

  function setValueParamFromDescription(text) {
    const id = valueParamId();
    if (id == null) return;
    const input = paramsBox.querySelector(`input[data-definition-id="${id}"]`);
    if (!input || input.value) return; // a unit-matched value already won
    const match = text.match(_BARE_ENGINEERING);
    if (!match) return;
    input.value = match[1] + ({ µ: "u", K: "k" }[match[2]] || match[2]);
  }

  const _EIA_PACKAGE = /\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b/;

  // Fill still-EMPTY fields from the description; never overwrite a structured
  // value (a shop that returns real parameters always wins).
  // `hints` is extra free text that is NOT the description — currently the shop's
  // own category name. It joins the package/mounting scan (TME files a 100nF part
  // under "MLCC SMD capacitors" while its description never says SMD) but is kept
  // out of the unit scan, where a category's stray digits could land in a number
  // field as a bogus measurement.
  function setFromDescription(text, hints) {
    const scan = [text, hints].filter(Boolean).join(" ");
    if (!scan) return;
    if (text) {
      for (const def of currentDefinitions) {
        if (def.data_type !== "number" || !def.unit) continue;
        const input = paramsBox.querySelector(`input[data-definition-id="${def.id}"]`);
        if (!input || input.value) continue;
        const value = findValueForUnit(text, def.unit);
        if (value) input.value = value;
      }
      setValueParamFromDescription(text); // last resort for a unitless value
    }

    const pkg = form.querySelector('[name="package"]');
    const eia = scan.match(_EIA_PACKAGE);
    if (pkg && !pkg.value && eia) pkg.value = eia[1];

    const mounting = form.querySelector('[name="mounting_type"]');
    let wanted = null;
    // Digi-Key spells it out ("Chip Resistor - Surface Mount") where TME abbreviates.
    if (/\bSM[DT]\b/.test(scan) || /surface[- ]mount/i.test(scan)) wanted = "SMT";
    else if (/\bTHT\b/.test(scan) || /through[- ]hole/i.test(scan)) wanted = "THT";
    if (mounting && wanted && [...mounting.options].some((o) => o.value === wanted)) {
      mounting.value = wanted;
    }
  }

  // Fill each named shop parameter into the field of the matching definition
  // (by label or name). Only plain <input> fields (number/text) are set; enum/bool
  // <select>s are left alone. NUMBER fields get engineering-cleaned; text fields get
  // the raw value. Unmatched params are dropped — best-effort, reviewed.
  function setNamedParams(params) {
    for (const { name, value } of params) {
      const target = normalizeName(name);
      if (!target) continue;
      const def = currentDefinitions.find(
        (d) => normalizeName(d.label) === target || normalizeName(d.name) === target,
      );
      if (!def) continue;
      const input = paramsBox.querySelector(`[data-definition-id="${def.id}"]`);
      if (input && input.tagName === "INPUT") {
        input.value = def.data_type === "number" ? cleanNumberValue(value) : value;
      }
    }
  }

  // Pre-fill the dialog. From a BOM line: { category, value, mpn, manufacturer }.
  // From a shop import, additionally: { notes, package, params:[{name,value}],
  // datasheetUrl }. Runs async (loads the matched type's parameters); fired after
  // the dialog is shown or when Import completes.
  async function applyPrefill(prefill) {
    pendingDatasheetUrl = null;
    if (!prefill) {
      loadParams(""); // clears the fields and shows the hint
      return;
    }
    const set = (name, val) => {
      if (val) form.querySelector(`[name="${name}"]`).value = val;
    };
    set("mpn", prefill.mpn);
    set("manufacturer", prefill.manufacturer);
    set("package", prefill.package);
    set("notes", prefill.notes);
    if (prefill.datasheetUrl) pendingDatasheetUrl = prefill.datasheetUrl;

    const typeId = matchTypeByName(prefill.category);
    if (!typeId) {
      loadParams(""); // no matching type — let the user pick one
      return;
    }
    typeSelect.value = typeId;
    await loadParams(typeId); // render the type's parameter fields
    // Only fill parameters if the user hasn't changed the type while we loaded.
    if (typeSelect.value !== typeId) return;
    if (prefill.value) setValueParam(prefill.value); // BOM: single value param
    if (prefill.params) setNamedParams(prefill.params); // shop: named params
    // Last: fills only what's still empty, from the description plus the shop's
    // own category text.
    setFromDescription(prefill.notes, prefill.shopCategory);
  }

  // "Import from a shop URL": look the part up via its shop's API and rich-prefill.
  const importUrl = document.getElementById("shop-import-url");
  const importBtn = document.getElementById("shop-import-btn");
  const importStatus = document.getElementById("shop-import-status");
  if (importBtn) {
    let importing = false;
    importBtn.addEventListener("click", async () => {
      const url = importUrl.value.trim();
      if (!url || importing) return;
      importing = true;
      const token = openToken; // ignore the result if the dialog is reopened
      importStatus.hidden = false;
      importStatus.className = "muted";
      importStatus.textContent = "Looking up…";
      try {
        const resp = await fetch("/api/shops/lookup", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body: JSON.stringify({ url }),
        });
        if (token !== openToken) return; // a different dialog session is open now
        if (!resp.ok) {
          importStatus.className = "error";
          importStatus.textContent = await errorMessage(resp);
          return;
        }
        const product = await resp.json();
        if (token !== openToken) return;
        await applyPrefill({
          category: product.category,
          mpn: product.mpn,
          manufacturer: product.manufacturer,
          notes: product.description,
          shopCategory: product.shop_category,
          package: product.package,
          params: product.parameters,
          datasheetUrl: product.datasheet_url,
        });
        importStatus.className = "muted";
        importStatus.textContent = product.mpn
          ? `Imported ${product.mpn} — review and Create.`
          : "Imported — review and Create.";
      } catch {
        importStatus.className = "error";
        importStatus.textContent = "Could not reach the server.";
      } finally {
        importing = false;
      }
    });
  }

  // Open the dialog; `callback(created)` runs after a successful create. An
  // optional `prefill` seeds the fields from a BOM line.
  window.openComponentDialog = function (callback, prefill) {
    onCreated = callback || null;
    openToken += 1; // invalidate any in-flight shop lookup from a prior open
    form.reset();
    errorEl.hidden = true;
    if (importStatus) importStatus.hidden = true;
    dialog.showModal(); // open synchronously; fields fill in a tick later
    applyPrefill(prefill);
  };
})();

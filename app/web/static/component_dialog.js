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
  // "Open in shop": opens the distributor product page for the part being created.
  // Enabled only when that URL is known — a shop import (scanned/pasted code) or an
  // invoice line supplies it; a manual or BOM create has none, so it stays disabled.
  const shopOpenBtn = document.getElementById("component-open-shop");

  // Monotonic id so overlapping type-select changes can't render stale fields.
  let paramsRequestId = 0;
  let onCreated = null;
  // When set ({invoiceId, importLineId}) the dialog is in "stage" mode: its submit
  // PATCHes a draft invoice's import line instead of creating a component (the
  // component is created later, at invoice finalize). Everything else — type, fields,
  // per-type parameters, "+ New type", description-based matching — is identical.
  let stageTarget = null;
  // The effective parameter definitions currently rendered (for prefill lookups).
  let currentDefinitions = [];
  // A datasheet URL from a shop import, attached to the component after it's created
  // (downloaded as a file if the shop allows it, else kept as a link).
  let pendingDatasheetUrl = null;
  // The shop URL the component was imported from: saved as a `shop` link on create,
  // and the target of the "Open in shop" button. Set it only through setShopUrl so
  // the button's enabled state always tracks it.
  let pendingShopUrl = null;
  function setShopUrl(url) {
    pendingShopUrl = url || null;
    if (shopOpenBtn) shopOpenBtn.disabled = !pendingShopUrl;
  }
  // "Open in shop" reuses ONE named tab ("shelfos-shop") rather than spawning a
  // fresh window each time: the first click opens a normal tab, which the user can
  // drag into their second window / split-view pane once, and every later click
  // navigates THAT same tab in place — so the shop page keeps landing where they put
  // it. Named-target reuse only works while the context stays script-reachable, so
  // no noopener/noreferrer (noreferrer implies noopener); the trade-off is the shop
  // page gets a window.opener handle, fine for distributor product links. A plain
  // name (no sizing/popup features) opens a real tab, not the stripped popup window.
  const SHOP_WINDOW_NAME = "shelfos-shop";
  if (shopOpenBtn) {
    shopOpenBtn.addEventListener("click", () => {
      if (pendingShopUrl) window.open(pendingShopUrl, SHOP_WINDOW_NAME);
    });
  }
  // The last shop-imported product, kept so switching type can re-run the engine.
  let lastImport = null;
  // Bumped on every open so a slow shop-lookup can't prefill a reopened dialog.
  let openToken = 0;
  // The shop-import runner, exposed here so openComponentDialog can fire it for a
  // code handed in at open time (a scan that matched no existing component). Stays
  // null on pages whose dialog has no import field (the BOM report reuses it).
  let triggerImport = null;
  // A lookup in flight. Module-scoped (not per shop-import session) so a fresh open
  // can RELEASE it — a lookup abandoned by closing the dialog must not wedge the
  // next scanned code out of ever being looked up. Its stale response is discarded
  // by the openToken check, so releasing the lock loses nothing.
  let importing = false;
  // Whether the "what didn't get filled" tint is showing. On only once something has
  // been prefilled — an import that fills six fields out of nine is what the tint is
  // for; a blank form the user opened themselves is not a form full of gaps.
  let showingGaps = false;

  // Every control the component itself is made of — selected as form elements rather
  // than by the .control class, so a field is covered whether or not it is styled
  // like one. The import box is excluded: it is the tool that fills the form, not a
  // field of the part, and it sits EMPTY exactly when an import has just filled
  // everything from a scanned code.
  function isComponentField(control) {
    return (
      control.id !== "shop-import-url" &&
      ["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName)
    );
  }

  function componentControls() {
    return [...form.querySelectorAll("input, select, textarea")].filter(
      isComponentField,
    );
  }

  // Whether a control still has nothing in it. Selects say so with an empty value —
  // the type picker's "Select a type…", a parameter's "—" — except mounting, whose
  // untouched state is the "Other" the markup preselects (the same sentinel
  // applyProposalFields treats as "nobody has set this").
  function isUnfilled(control) {
    if (control.name === "mounting_type") return control.value === "Other";
    return !control.value;
  }

  // Tint every field left unfilled, and untint the rest. Advisory only — nothing is
  // required and nothing is blocked; the point is that a prefilled form shows its
  // gaps at a glance instead of hiding them among the values that did arrive.
  function refreshGaps() {
    for (const control of componentControls()) {
      control.classList.toggle("is-unfilled", showingGaps && isUnfilled(control));
    }
  }

  // A field the user has just typed in or picked from stops being a gap immediately,
  // rather than at some later refresh. Delegated, because a type's parameter fields
  // are built after this runs — and listening for both events covers a select
  // (change) and text typed a character at a time (input).
  for (const event of ["input", "change"]) {
    form.addEventListener(event, (e) => {
      const control = e.target;
      if (!control.tagName || !isComponentField(control)) return;
      control.classList.toggle("is-unfilled", showingGaps && isUnfilled(control));
    });
  }

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

  // Wrapped for the same reason applyPrefill is: the tint has to be right however
  // this returns, and there are four ways out — no type, a failed fetch, a type with
  // no parameters, a newer selection overtaking this one. Hanging the refresh off
  // the one exit that renders fields left the other three able to miss it.
  async function loadParams(typeId) {
    try {
      return await loadParamsFields(typeId);
    } finally {
      refreshGaps();
    }
  }

  async function loadParamsFields(typeId) {
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

  typeSelect.addEventListener("change", async (event) => {
    const typeId = event.target.value;
    await loadParams(typeId);
    // After a shop import, picking a different type refills its parameters from the
    // engine (the import filled the auto-inferred type; a correction should too).
    if (lastImport && typeId) {
      const proposal = await fetchProposal(typeId, lastImport);
      if (proposal && typeSelect.value === typeId) applyProposal(proposal);
    }
  });

  // Download a datasheet URL as a file attachment via the SSRF-guarded endpoint.
  // Returns true on success; a non-2xx or a network error is a handled false, never
  // an exception (the component already exists).
  async function attachDatasheet(componentId, url) {
    try {
      const resp = await fetch("/api/attachments/from-url", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          entity_type: "component",
          entity_id: componentId,
          url,
          kind: "datasheet",
        }),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }

  // Save an external link on the component. Best-effort like the datasheet download.
  async function addLink(componentId, kind, url) {
    try {
      const resp = await fetch("/api/links", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          entity_type: "component",
          entity_id: componentId,
          kind,
          url,
        }),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }

  // Render a failed-create response. A duplicate (409) carries `existing_id`, so
  // the message gets a clickable link to the component that already exists; every
  // other failure is plain text. Built with DOM nodes (not innerHTML) so the
  // server text can't inject markup.
  function showCreateError(body) {
    errorEl.replaceChildren();
    // Number(...) guards the href: existing_id is always a server-issued int, but
    // never build a path from an unchecked value.
    const existingId = body && Number(body.existing_id);
    if (existingId) {
      errorEl.append(document.createTextNode(errorTextFromBody(body) + " "));
      const link = document.createElement("a");
      link.href = `/components/${existingId}`;
      link.textContent = "View the existing component";
      errorEl.append(link);
    } else {
      errorEl.textContent = errorTextFromBody(body, "Could not create the component.");
    }
    errorEl.hidden = false;
  }

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

      // Stage mode: save the review edits to the import line and stop — no component
      // is created here (it is made at finalize). Skips the create-only follow-ups
      // (datasheet/links) since there is no component id to attach them to yet.
      if (stageTarget) {
        let resp;
        try {
          resp = await fetch(
            `/api/invoices/${stageTarget.invoiceId}/import-lines/${stageTarget.importLineId}`,
            {
              method: "PATCH",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken,
              },
              body: JSON.stringify({
                type_id: typeSelect.value ? Number(typeSelect.value) : null,
                manufacturer: value("manufacturer") || null,
                mpn: value("mpn") || null,
                package: value("package") || null,
                mounting_type: form.querySelector('[name="mounting_type"]').value,
                description: value("notes") || null,
                parameters: collectParameters(),
              }),
            },
          );
        } catch {
          errorEl.textContent = "Could not reach the server. Please try again.";
          errorEl.hidden = false;
          return;
        }
        if (!resp.ok) {
          showCreateError(await resp.json().catch(() => null));
          return;
        }
        dialog.close();
        if (onCreated) {
          try {
            onCreated(await resp.json());
          } catch {
            /* swallow — the row was saved; only the caller's hook failed */
          }
        }
        return;
      }

      let created;
      try {
        const resp = await fetch("/api/components", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body,
        });
        if (!resp.ok) {
          showCreateError(await resp.json().catch(() => null));
          return;
        }
        created = await resp.json();
      } catch {
        errorEl.textContent = "Could not reach the server. Please try again.";
        errorEl.hidden = false;
        return;
      }
      // Capture what a shop import gave us. Each step is best-effort — none may undo
      // the already-created component — but nothing is lost silently: a step that
      // fails outright is reported, and a datasheet kept as a link is reported too.
      let datasheetLinked = false;
      const lost = [];
      if (created && created.id) {
        // Keep the shop page the part came from as a link (the original motivation).
        if (pendingShopUrl && !(await addLink(created.id, "shop", pendingShopUrl))) {
          lost.push("the shop link");
        }
        // Try to download the datasheet as a file (Mouser/Digi-Key allow it). If the
        // shop blocks server-side fetches — TME's document host answers a Cloudflare
        // challenge — keep it as a datasheet LINK instead. Only if BOTH fail is it
        // lost, and even then the user is told.
        if (pendingDatasheetUrl) {
          if (await attachDatasheet(created.id, pendingDatasheetUrl)) {
            // Downloaded as a file — nothing to report.
          } else if (await addLink(created.id, "datasheet", pendingDatasheetUrl)) {
            datasheetLinked = true;
          } else {
            lost.push("the datasheet");
          }
        }
      }
      // Used; clear so a later manual (non-import) create can't reuse a stale URL.
      setShopUrl(null);
      pendingDatasheetUrl = null;
      dialog.close();
      if (lost.length) {
        // "and", not "or": with both items lost, "or" would read as though one of
        // them was saved.
        showToast(`Component created, but couldn't save ${lost.join(" and ")}.`);
      } else if (datasheetLinked) {
        showToast(
          "Component created. Its datasheet couldn't be downloaded (the shop blocks " +
            "automated downloads), so it was saved as a link instead.",
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

  // Pre-fill the dialog. From a BOM line: { category, value, mpn, manufacturer }.
  // From a shop import, additionally: { notes, package, proposal }, where the SERVER's
  // matching engine already worked out the type, mounting and parameter values (the
  // dialog no longer guesses). Runs async (loads the type's parameters); fired after
  // the dialog is shown or when Import completes.
  async function applyPrefill(prefill) {
    // The tint belongs to prefilled forms only, and it is refreshed however the
    // inner function returns — several of its paths give up early (no type matched,
    // the type changed while its parameters loaded), and each of those leaves
    // exactly the gaps worth pointing at.
    showingGaps = !!prefill;
    try {
      await applyPrefillFields(prefill);
    } finally {
      refreshGaps();
    }
  }

  async function applyPrefillFields(prefill) {
    pendingDatasheetUrl = null;
    // An invoice line's prefill carries a prebuilt shop URL; a BOM/blank one has
    // none. A later shop lookup (runImport) overrides this with its source_url.
    setShopUrl(prefill && prefill.shopUrl);
    lastImport = null; // no shop import behind a BOM/blank prefill
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

    const proposal = prefill.proposal || null;
    // Package and mounting are type-independent, so apply them up front — before the
    // "no type resolved" early return below. The engine still works them out for a
    // part whose category doesn't map to one of our types (e.g. an IC the shop
    // labels "SMD/SMT"), and the user shouldn't lose that guess just because they
    // have to pick the type by hand. Parameter values need the type's fields and are
    // applied further down, once loaded.
    if (proposal) applyProposalFields(proposal);
    // A staged review row carries a mounting the engine already inferred. Apply it up
    // front too, before the type gate: an invoice line whose type didn't resolve
    // arrives with typeId "" (styled is-incomplete), and would otherwise hit the
    // early return below and lose its mounting — the same bug, one field over. Applied
    // after applyProposalFields, so a staged mounting still wins over the proposal's.
    if (prefill.mountingType) {
      const mounting = form.querySelector('[name="mounting_type"]');
      if (mounting && [...mounting.options].some((o) => o.value === prefill.mountingType)) {
        mounting.value = prefill.mountingType;
      }
    }
    // The type is known by: a staged line's id, else the engine's proposal, else a
    // BOM prefill's category name.
    const typeId =
      prefill.typeId != null && prefill.typeId !== ""
        ? String(prefill.typeId)
        : proposal && proposal.type_id != null
          ? String(proposal.type_id)
          : matchTypeByName(prefill.category);
    if (!typeId) {
      loadParams(""); // no matching type — let the user pick one
      return;
    }
    typeSelect.value = typeId;
    await loadParams(typeId); // render the type's parameter fields
    // Only fill parameters if the user hasn't changed the type while we loaded.
    if (typeSelect.value !== typeId) return;
    if (prefill.value) setValueParam(prefill.value); // BOM: single value param
    if (proposal) applyProposal(proposal); // shop: the engine's field guesses
    // Values the user already entered in a prior review edit win over the guesses.
    if (prefill.paramValues) setParamsById(prefill.paramValues);
  }

  // The type-independent half of a proposal: package and mounting. Never overwrites
  // a package or mounting the user already set — switching type re-runs the engine,
  // and mounting is type-independent, so a manual correction must survive the
  // re-fetch. "Other" is the default/untouched sentinel. Idempotent: re-applying
  // finds the field already filled and leaves it, so callers may run it more than
  // once (applyPrefill applies it early, then applyProposal again after the type).
  function applyProposalFields(proposal) {
    if (proposal.package) {
      const pkg = form.querySelector('[name="package"]');
      if (pkg && !pkg.value) pkg.value = proposal.package;
    }
    if (proposal.mounting_type) {
      const mounting = form.querySelector('[name="mounting_type"]');
      const ok =
        mounting &&
        mounting.value === "Other" &&
        [...mounting.options].some((o) => o.value === proposal.mounting_type);
      if (ok) mounting.value = proposal.mounting_type;
    }
  }

  // Apply the engine's proposal to the form: package, mounting, and parameter values
  // (by definition id). The parameter half needs the type's fields already rendered.
  function applyProposal(proposal) {
    applyProposalFields(proposal);
    if (proposal.parameters) setParamsById(proposal.parameters);
    // Setting .value in script fires no input event, so the tint is refreshed by
    // hand — this is the call that clears it off everything the engine did fill.
    refreshGaps();
  }

  // Re-run the engine server-side for a chosen type over the last imported product,
  // so switching type refills the parameter fields. Returns a proposal or null.
  async function fetchProposal(typeId, product) {
    try {
      const resp = await fetch("/api/matching/proposal", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          type_id: Number(typeId),
          category: product.category,
          shop_category: product.shop_category,
          description: product.description,
          package: product.package,
          parameters: product.parameters,
        }),
      });
      return resp.ok ? await resp.json() : null;
    } catch {
      return null;
    }
  }

  // Set parameter inputs from stored [{parameter_definition_id, value}] pairs.
  function setParamsById(pairs) {
    for (const pair of pairs) {
      const input = paramsBox.querySelector(
        `[data-definition-id="${pair.parameter_definition_id}"]`,
      );
      if (!input) continue;
      input.value =
        input.dataset.dataType === "bool"
          ? pair.value === true || pair.value === "true"
            ? "true"
            : "false"
          : String(pair.value);
    }
  }

  // "+ New type": open the New Type dialog (stacked on this one) and, on create,
  // add the new type to the select, choose it, and render its parameter fields.
  // Gated on the type dialog being ON THE PAGE (not on window.openTypeDialog, which
  // type_dialog.js may define after this script runs) so the button hides on pages —
  // the BOM report — that reuse this dialog without a type builder.
  const newTypeBtn = document.getElementById("component-new-type");
  if (newTypeBtn && document.getElementById("type-dialog")) {
    newTypeBtn.hidden = false;
    newTypeBtn.addEventListener("click", () => {
      window.openTypeDialog?.((created) => {
        let option = [...typeSelect.options].find(
          (o) => o.value === String(created.id),
        );
        if (!option) {
          option = document.createElement("option");
          option.value = created.id;
          option.textContent = created.name;
          typeSelect.appendChild(option);
        }
        typeSelect.value = String(created.id);
        loadParams(String(created.id));
      });
    });
  }

  // "Import from a shop URL or a scanned code": look the part up via its shop's API
  // and rich-prefill. The same field takes a pasted URL and a scanned barcode/QR.
  const importUrl = document.getElementById("shop-import-url");
  const importBtn = document.getElementById("shop-import-btn");
  const importStatus = document.getElementById("shop-import-status");
  if (importBtn) {
    const runImport = async () => {
      const code = importUrl.value.trim();
      if (!code) return;
      if (importing) {
        // Two scans in quick succession is the normal wedge-scanner mishap; say
        // which one is landing rather than dropping the second without a word.
        importStatus.hidden = false;
        importStatus.className = "error";
        importStatus.textContent = "Still looking the previous code up — try again.";
        return;
      }
      importing = true;
      const token = openToken; // ignore the result if the dialog is reopened
      importStatus.hidden = false;
      importStatus.className = "muted";
      importStatus.textContent = "Looking up…";
      try {
        const resp = await fetch("/api/shops/lookup", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
          body: JSON.stringify({ code }),
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
          package: product.package,
          datasheetUrl: product.datasheet_url,
          proposal: product.proposal,
        });
        // applyPrefill cleared these; restore the shop link from the URL the SERVER
        // resolved — a TME QR buries its URL among other tokens, and a barcode has
        // none at all, so the raw code must never be stored as a link — and keep the
        // product so a later type change can re-run the engine. (Also enables the
        // "Open in shop" button.)
        setShopUrl(product.source_url);
        lastImport = product;
        // "warn", not "error": the import partly succeeded and the fields ARE
        // filled — red is reserved for the cases where nothing landed.
        importStatus.className = product.from_label_only ? "warn" : "muted";
        if (product.from_label_only) {
          // The shop's API added nothing (no key, or the lookup failed); the fields
          // come off the label alone, so say so rather than implying a full import.
          importStatus.textContent =
            "The shop lookup failed — filled from the scanned label only.";
        } else {
          importStatus.textContent = product.mpn
            ? `Imported ${product.mpn} — review and Create.`
            : "Imported — review and Create.";
        }
      } catch {
        importStatus.className = "error";
        importStatus.textContent = "Could not reach the server.";
      } finally {
        importing = false;
      }
    };
    triggerImport = runImport; // so openComponentDialog can run it for a scanned code
    importBtn.addEventListener("click", runImport);
    // A keyboard-wedge scanner ends its payload with Enter, so a scan imports by
    // itself. preventDefault so that Enter doesn't submit the half-empty form.
    importUrl.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      runImport();
    });
  }

  // Open the dialog; `callback(result)` runs after a successful create (or, in stage
  // mode, save). `prefill` seeds the fields (BOM line, shop import, or a staged import
  // line by {typeId, paramValues}). `opts.stage = {invoiceId, importLineId}` switches
  // to stage mode (PATCH the import line instead of creating a component).
  // `opts.importCode` pre-fills the import field and looks it up straight away — the
  // components-page scan hands over a code that matched no existing component, so the
  // part is imported without the user rescanning it.
  window.openComponentDialog = function (callback, prefill, opts) {
    const importCode = opts && opts.importCode;
    // A scan can arrive while the dialog from a PREVIOUS scan is still up (the
    // components page queues a bag scanned mid-lookup, and drains it once this
    // dialog is open). Reopening then would call showModal() on an already-open
    // dialog — an InvalidStateError — after form.reset() had wiped the first
    // import, surfacing as a bogus "Could not reach the server." So when already
    // open, don't reopen: just swap in the new code and look it up, replacing the
    // bag under review. (Only the import path can reopen; every other caller
    // fires from a closed dialog.)
    const reopening = dialog.open;
    onCreated = callback || null;
    // Off until something is prefilled, and repainted on the spot — clearing the
    // flag alone would leave the previous session's tint sitting on the form. That
    // is the reopen case (a bag scanned while the previous one is still up): the
    // form is reset below and bag B's lookup takes a moment, and bag A's gaps
    // marked against bag B's number read as information, which is worse than noise.
    showingGaps = false;
    refreshGaps();
    openToken += 1; // invalidate any in-flight shop lookup from a prior open
    importing = false; // …and release its lock so the new code is looked up
    errorEl.hidden = true;
    if (!reopening) {
      stageTarget = (opts && opts.stage) || null;
      form.reset();
      if (importStatus) importStatus.hidden = true;
      dialog.showModal(); // open synchronously; fields fill in a tick later
      // Focus the import field so a scanner's payload lands there without a click.
      if (importUrl) importUrl.focus();
      // applyPrefill(null) returns synchronously (loadParams("") early-returns
      // before its first await), so this unawaited call cannot race the import's
      // own applyPrefill below and clobber the fields the lookup fills in.
      applyPrefill(prefill);
    }
    if (importCode && importUrl && triggerImport) {
      // On a reopen the reset block above is skipped, so the previous bag's fields
      // are still in the form — and the new lookup's applyPrefill only OVERWRITES
      // truthy values, so a label-only import (the ordinary no-API-key case) would
      // inherit bag A's manufacturer/package/description under bag B's number. Clear
      // them, and the type's parameter inputs, so only what the new lookup fills
      // survives. (setShopUrl(null) likewise drops the prior bag's shop link until
      // this code resolves.)
      if (reopening) {
        form.reset();
        loadParams(""); // clear the previous type's parameter fields too
      }
      setShopUrl(null);
      importUrl.value = importCode;
      triggerImport();
    }
  };
})();

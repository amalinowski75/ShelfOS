import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, matchRulesPageFixture } from "./harness.js";

// The create dialog moved to match_rule_dialog.js (shared so the type builder can open
// it too); match_rules.js keeps the table + inline editing and wires the "New rule"
// button to window.openMatcherDialog. Load both, dialog script first.
const SCRIPTS = ["shared.js", "match_rule_dialog.js", "match_rules.js"];

// A minimal Tabulator cell double: the value being edited + its row's data, plus a
// spy for the revert the code calls when it will not send an edit at all.
//
// A row is a TARGET, not a rule: it carries the rules its alias list stands for, which
// is what an edit writes to. `groupRow` builds one the way groupRulesByTarget does.
function editCell(value, row = groupRow([{ id: 7, alias: "x" }])) {
  return {
    getValue: () => value,
    getRow: () => ({ getData: () => row }),
    restoreOldValue: vi.fn(),
  };
}

function groupRow(rules, extra = {}) {
  return {
    id: rules[0].id,
    domain: "type",
    canonical: "resistor",
    parameter_definition_id: null,
    sort_order: 0,
    ...extra,
    rules,
    alias: rules.map((r) => r.alias).join(", "),
  };
}

function submit(document, formId) {
  document
    .getElementById(formId)
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

function fire(el, type) {
  el.dispatchEvent(
    new el.ownerDocument.defaultView.Event(type, { cancelable: true, bubbles: true }),
  );
}

// The first fetch call using the given HTTP method (the page also fetches /api/types
// at load to ready the inline Target editor, so writes aren't always call 0).
function writeCall(fetchMock, method) {
  return fetchMock.mock.calls.find((c) => c[1]?.method === method);
}

describe("match_rules.js — columns", () => {
  it("labels the columns and escapes the editable text", () => {
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const columns = window.ruleColumns();
    expect(columns.map((c) => c.field)).toEqual([
      "domain",
      "alias",
      "canonical",
      "parameter",
      "sort_order",
      "actions",
    ]);
    // Alias/target render escaped so a stray "<" can't inject markup.
    const alias = columns.find((c) => c.field === "alias");
    expect(alias.formatter(editCell("<b>x"))).toBe(
      '<span class="cell-mono">&lt;b&gt;x</span>',
    );
    // A global rule has no parameter scope — the cell renders empty, not "null".
    const param = columns.find((c) => c.field === "parameter");
    expect(param.formatter(editCell(null))).toBe("");
    expect(param.formatter(editCell("resistor / Resistance"))).toBe(
      "resistor / Resistance",
    );
  });

  it("marks alias/target/order editable but leaves domain and parameter fixed", () => {
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const byField = Object.fromEntries(
      window.ruleColumns().map((c) => [c.field, c]),
    );
    expect(byField.alias.editor).toBe("input");
    expect(byField.canonical.editor).toBe("list"); // constrained per domain
    expect(byField.sort_order.editor).toBe("number");
    expect(byField.domain.editor).toBeUndefined();
    expect(byField.parameter.editor).toBeUndefined();
    // Order's semantics (lower wins on a tie) are non-obvious, so the header says so.
    expect(byField.sort_order.headerTooltip).toMatch(/lower wins/i);
  });

  it("constrains the inline Target editor to the row's domain vocabulary", async () => {
    const fetchImpl = (url) => {
      if (url === "/api/types") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 1, name: "resistor" }, { id: 2, name: "diode" }],
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({ data: [] }) });
    };
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS, { fetchImpl });
    await tick(); // let the startup /api/types fetch populate the type list
    const target = window.ruleColumns().find((c) => c.field === "canonical");
    const paramsFor = (row) =>
      target.editorParams({ getRow: () => ({ getData: () => row }) });

    // Mounting → the fixed enum list, no free text.
    expect(paramsFor({ domain: "mounting" })).toEqual({
      values: ["SMT", "THT", "Other", "Panel", "Wire"],
    });
    // Type → the existing type names.
    expect(paramsFor({ domain: "type" })).toEqual({ values: ["resistor", "diode"] });
    // enum_value → the row's own allowed values (shipped by the feed), NOT free text —
    // a free-typed enum target could never fire, so it must not be enterable.
    expect(
      paramsFor({ domain: "enum_value", enum_values: ["ribbon", "coax"] }),
    ).toEqual({ values: ["ribbon", "coax"] });
    // param_name → free text (a definition name). Tabulator's list editor only accepts
    // typed text with `autocomplete` on, so both flags are set — a bare `freetext`
    // leaves the cell un-editable.
    expect(paramsFor({ domain: "param_name" })).toEqual({
      values: [],
      autocomplete: true,
      freetext: true,
      listOnEmpty: true,
    });
  });
});

describe("match_rules.js — inline edit", () => {
  it("renames an alias in place when the list swaps one word", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const alias = window.ruleColumns().find((c) => c.field === "alias");
    await alias.cellEdited(
      editCell("opornik", groupRow([{ id: 42, alias: "rezystor" }])),
    );

    const patch = writeCall(fetchMock, "PATCH");
    expect(patch[0]).toBe("/api/admin/match-rules/42");
    expect(patch[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(patch[1].body)).toEqual({ alias: "opornik" });
  });

  it("sends the order as a number, to every rule under the target", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const order = window.ruleColumns().find((c) => c.field === "sort_order");
    await order.cellEdited(
      editCell("5", groupRow([{ id: 3, alias: "a" }, { id: 4, alias: "b" }])),
    );
    const patches = fetchMock.mock.calls.filter((c) => c[1]?.method === "PATCH");
    // Order belongs to the target, not to one of its spellings, so both move.
    expect(patches.map((c) => c[0])).toEqual([
      "/api/admin/match-rules/3",
      "/api/admin/match-rules/4",
    ]);
    expect(JSON.parse(patches[0][1].body)).toEqual({ sort_order: 5 });
  });

  it("alerts when the server rejects the edit", async () => {
    // The duplicate-alias guard the admin asked for surfaces here.
    const fetchImpl = (url) =>
      url === "/web/api/match-rules"
        ? Promise.resolve({ ok: true, json: async () => ({ data: [] }) })
        : Promise.resolve({
            ok: false,
            json: async () => ({ detail: "an identical rule already exists" }),
          });
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS, { fetchImpl });
    window.alert = vi.fn();
    const alias = window.ruleColumns().find((c) => c.field === "alias");
    await alias.cellEdited(editCell("smd", groupRow([{ id: 9, alias: "smt" }])));

    expect(window.alert).toHaveBeenCalledWith("an identical rule already exists");
  });

  it("will not let the list be emptied — that is what Delete is for", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);
    window.alert = vi.fn();
    const cell = editCell(" , ", groupRow([{ id: 9, alias: "smt" }]));
    const alias = window.ruleColumns().find((c) => c.field === "alias");
    await alias.cellEdited(cell);

    // Nothing written, and the cell put back rather than the target wiped out.
    expect(writeCall(fetchMock, "DELETE")).toBeUndefined();
    expect(writeCall(fetchMock, "PATCH")).toBeUndefined();
    expect(cell.restoreOldValue).toHaveBeenCalled();
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("at least one"));
  });
});

describe("match_rules.js — loading & delete", () => {
  it("fetches the feed and shows one row per target", async () => {
    // Three rules, two targets: the two type aliases are one row, and the rule
    // scoped to a parameter stays its own however its target reads.
    const feed = {
      data: [
        { id: 1, domain: "type", alias: "rezystor", canonical: "resistor", parameter_definition_id: null, sort_order: 5 },
        { id: 2, domain: "type", alias: "opornik", canonical: "resistor", parameter_definition_id: null, sort_order: 2 },
        { id: 3, domain: "enum_value", alias: "NP0", canonical: "C0G", parameter_definition_id: 9, sort_order: 0 },
      ],
    };
    const fetchImpl = () => Promise.resolve({ ok: true, json: async () => feed });
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.spyOn(window.Tabulator.prototype, "setData");

    await window.loadRules();
    await tick();

    const shown = setData.mock.calls[0][0];
    expect(shown.map((r) => [r.alias, r.canonical])).toEqual([
      ["rezystor, opornik", "resistor"],
      ["NP0", "C0G"],
    ]);
    // The row keeps the rules it stands for, and takes the LOWEST order of them —
    // the engine stops at the first match, so that is the one that decides.
    expect(shown[0].rules.map((r) => r.id)).toEqual([1, 2]);
    expect(shown[0].sort_order).toBe(2);
  });

  it("deletes only after confirmation, and takes every alias with it", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const row = groupRow([{ id: 5, alias: "x" }, { id: 6, alias: "y" }], {
      canonical: "ic",
    });

    window.confirm = vi.fn(() => false);
    window.deleteRule(row);
    await tick();
    // Declining the confirm sends no DELETE (the startup /api/types fetch aside).
    expect(writeCall(fetchMock, "DELETE")).toBeUndefined();

    window.confirm = vi.fn(() => true);
    window.deleteRule(row);
    await tick();
    await tick();
    // The prompt counts the aliases, because the row shows a list and the button
    // gives no other hint of how much it removes.
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("all 2 aliases"));
    expect(
      fetchMock.mock.calls.filter((c) => c[1]?.method === "DELETE").map((c) => c[0]),
    ).toEqual(["/api/admin/match-rules/5", "/api/admin/match-rules/6"]);
  });
});

describe("match_rules.js — create", () => {
  // The dialog needs the type list even for its default (type) domain, so every
  // create test serves /api/types.
  const withTypes = (types) => (url) => {
    if (url === "/api/types") {
      return Promise.resolve({ ok: true, json: async () => types });
    }
    if (url.endsWith("/parameters")) {
      return Promise.resolve({ ok: true, json: async () => [] });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  };

  it("picks a mounting target from the enum select, not free text", async () => {
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: withTypes([]),
    });
    document.getElementById("rule-new-btn").click();
    await tick();
    const form = document.getElementById("rule-new-form");
    form.elements.domain.value = "mounting";
    fire(form.elements.domain, "change");
    await tick();
    // The mounting target is the enum select; the type list and free text are hidden.
    expect(form.elements.canonical_mounting.hidden).toBe(false);
    expect(form.elements.canonical_type.hidden).toBe(true);
    expect(form.elements.canonical_text.hidden).toBe(true);

    form.elements.alias.value = "  przewlekany  ";
    form.elements.canonical_mounting.value = "THT";
    form.elements.sort_order.value = "2";
    submit(document, "rule-new-form");
    await tick();

    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/admin/match-rules");
    expect(post[1].method).toBe("POST");
    expect(JSON.parse(post[1].body)).toEqual({
      domain: "mounting",
      alias: "przewlekany", // trimmed
      canonical: "THT",
      sort_order: 2,
      parameter_definition_id: null,
    });
  });

  it("offers existing types as the target for a type rule", async () => {
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: withTypes([
        { id: 1, name: "resistor" },
        { id: 2, name: "capacitor" },
      ]),
    });
    document.getElementById("rule-new-btn").click();
    await tick();
    await tick();
    const form = document.getElementById("rule-new-form");
    // Default domain is "type": the target is a list of the existing type NAMES.
    expect(form.elements.canonical_type.hidden).toBe(false);
    expect([...form.elements.canonical_type.options].map((o) => o.value)).toEqual([
      "resistor",
      "capacitor",
    ]);

    form.elements.alias.value = "opornik";
    form.elements.canonical_type.value = "resistor";
    submit(document, "rule-new-form");
    await tick();

    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/admin/match-rules");
    expect(JSON.parse(post[1].body)).toMatchObject({
      domain: "type",
      alias: "opornik",
      canonical: "resistor", // the type name, from the select
      parameter_definition_id: null,
    });
  });

  it("loads types/params for a scoped domain and posts the parameter id", async () => {
    const fetchImpl = (url) => {
      if (url === "/api/types") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 1, name: "resistor" }],
        });
      }
      if (url === "/api/types/1/parameters") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 10, label: "Resistance" }],
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("rule-new-btn").click();
    await tick();
    const form = document.getElementById("rule-new-form");
    // Switch to a scoped domain: the type/param pickers appear and populate.
    form.elements.domain.value = "param_name";
    fire(form.elements.domain, "change");
    await tick();
    await tick();

    expect(document.getElementById("rule-scope-type").hidden).toBe(false);
    expect(form.elements.parameter.value).toBe("10");
    // A param_name rule's target is free text (the parameter's canonical name).
    expect(form.elements.canonical_text.hidden).toBe(false);

    form.elements.alias.value = "Rezystancja";
    form.elements.canonical_text.value = "resistance";
    submit(document, "rule-new-form");
    await tick();

    const post = fetchMock.mock.calls.find(
      (c) => c[0] === "/api/admin/match-rules",
    );
    expect(JSON.parse(post[1].body)).toEqual({
      domain: "param_name",
      alias: "Rezystancja",
      canonical: "resistance",
      sort_order: 0,
      parameter_definition_id: 10,
    });
  });

  it("rebinds the parameter to the reset type when the dialog is reopened", async () => {
    const fetchImpl = (url) => {
      if (url === "/api/types") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 1, name: "resistor" }, { id: 2, name: "cable" }],
        });
      }
      if (url === "/api/types/1/parameters") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 10, label: "Resistance" }],
        });
      }
      if (url === "/api/types/2/parameters") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 20, label: "Jacket" }],
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    const form = document.getElementById("rule-new-form");

    // First open: scope the rule to a CABLE parameter (id 20).
    document.getElementById("rule-new-btn").click();
    await tick();
    form.elements.domain.value = "param_name";
    fire(form.elements.domain, "change");
    await tick();
    await tick();
    form.elements.type.value = "2"; // cable
    fire(form.elements.type, "change");
    await tick();
    expect(form.elements.parameter.value).toBe("20");
    document.getElementById("rule-new-dialog").close();

    // Reopen: form.reset() snaps Type back to resistor WITHOUT firing `change`. The
    // parameter picker must follow to resistor's param, not keep cable's stale 20.
    document.getElementById("rule-new-btn").click();
    await tick();
    await tick();
    form.elements.domain.value = "param_name";
    fire(form.elements.domain, "change");
    await tick();
    await tick();
    expect(form.elements.type.value).toBe("1"); // resistor
    expect(form.elements.parameter.value).toBe("10"); // resistor's, not cable's 20

    form.elements.alias.value = "Rezystancja";
    form.elements.canonical_text.value = "resistance";
    submit(document, "rule-new-form");
    await tick();
    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/admin/match-rules");
    expect(JSON.parse(post[1].body).parameter_definition_id).toBe(10);
  });

  it("offers the parameter's enum values as the target for an enum_value rule", async () => {
    const fetchImpl = (url) => {
      if (url === "/api/types") {
        return Promise.resolve({
          ok: true,
          json: async () => [{ id: 1, name: "cable" }],
        });
      }
      if (url === "/api/types/1/parameters") {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: 20,
              label: "Type",
              data_type: "enum",
              enum_values: ["ribbon", "coax", "power"],
            },
            { id: 21, label: "Length", data_type: "number", enum_values: [] },
          ],
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("rule-new-btn").click();
    await tick();
    await tick();
    const form = document.getElementById("rule-new-form");
    form.elements.domain.value = "enum_value";
    fire(form.elements.domain, "change");
    await tick();

    // Only the enum parameter is offered (Length, a number, is filtered out).
    expect([...form.elements.parameter.options].map((o) => o.value)).toEqual(["20"]);
    // The Target is a dropdown of that parameter's allowed values, not free text.
    expect(form.elements.canonical_enum.hidden).toBe(false);
    expect(form.elements.canonical_text.hidden).toBe(true);
    expect([...form.elements.canonical_enum.options].map((o) => o.value)).toEqual([
      "ribbon",
      "coax",
      "power",
    ]);

    form.elements.alias.value = "taśma";
    form.elements.canonical_enum.value = "coax";
    submit(document, "rule-new-form");
    await tick();

    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/admin/match-rules");
    expect(JSON.parse(post[1].body)).toEqual({
      domain: "enum_value",
      alias: "taśma",
      canonical: "coax", // chosen from the parameter's allowed values
      sort_order: 0,
      parameter_definition_id: 20,
    });
  });

  it("refuses a scoped rule with no parameter chosen", async () => {
    // No fetchImpl for types → the param select stays empty; submit must not POST.
    const fetchImpl = (url) => {
      if (url === "/api/types") return Promise.resolve({ ok: true, json: async () => [] });
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("rule-new-btn").click();
    await tick();
    const form = document.getElementById("rule-new-form");
    form.elements.domain.value = "enum_value";
    fire(form.elements.domain, "change");
    await tick();
    form.elements.alias.value = "czerwony";
    form.elements.canonical_text.value = "red";

    submit(document, "rule-new-form");
    await tick();

    expect(
      fetchMock.mock.calls.some((c) => c[0] === "/api/admin/match-rules"),
    ).toBe(false);
    expect(document.getElementById("rule-new-error").hidden).toBe(false);
  });
});

describe("match_rule_dialog.js — openMatcherDialog(prefill)", () => {
  // A caller (the type builder / a type's parameter list) opens the dialog already
  // pointed at one parameter, so nothing has to be re-picked.
  function scopedFetch() {
    return (url) => {
      if (url === "/api/types") {
        return Promise.resolve({
          ok: true,
          json: async () => [
            { id: 1, name: "resistor" },
            { id: 2, name: "cable" },
          ],
        });
      }
      if (url === "/api/types/1/parameters") {
        return Promise.resolve({
          ok: true,
          json: async () => [
            { id: 10, name: "resistance", label: "Resistance", data_type: "number", enum_values: [] },
            { id: 11, name: "tolerance", label: "Tolerance", data_type: "number", enum_values: [] },
          ],
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
  }

  it("pre-scopes domain, type and parameter, and defaults the target to the param name", async () => {
    const { window, document } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: scopedFetch(),
    });
    await window.openMatcherDialog(null, {
      domain: "param_name",
      typeId: 1,
      parameterDefinitionId: 11,
    });
    await tick();
    const form = document.getElementById("rule-new-form");
    expect(form.elements.domain.value).toBe("param_name");
    expect(form.elements.type.value).toBe("1");
    expect(form.elements.parameter.value).toBe("11");
    // param_name maps a label onto the parameter's own (technical) name.
    expect(form.elements.canonical_text.value).toBe("tolerance");
  });

  it("submitting the pre-scoped rule POSTs that parameter id and fires onCreated", async () => {
    const created = [];
    const { window, document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: scopedFetch(),
    });
    await window.openMatcherDialog((c) => created.push(c), {
      domain: "param_name",
      typeId: 1,
      parameterDefinitionId: 10,
    });
    await tick();
    const form = document.getElementById("rule-new-form");
    form.elements.alias.value = "Rezystancja";
    submit(document, "rule-new-form");
    await tick();
    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/admin/match-rules");
    expect(JSON.parse(post[1].body)).toMatchObject({
      domain: "param_name",
      alias: "Rezystancja",
      canonical: "resistance",
      parameter_definition_id: 10,
    });
    expect(created.length).toBe(1); // onCreated ran
  });
});

describe("match_rules.js — creating a whole vocabulary at once", () => {
  // The dialog needs the type list even for its default (type) domain.
  const serve = (onPost) => (url, opts) => {
    if (url === "/api/types") {
      return Promise.resolve({ ok: true, json: async () => [{ id: 1, name: "resistor" }] });
    }
    if (url === "/api/admin/match-rules" && opts?.method === "POST") {
      return onPost(JSON.parse(opts.body));
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  };
  const okResp = () => Promise.resolve({ ok: true, json: async () => ({ id: 1 }) });

  const openWith = async (document, aliasText, fetchExtras = {}) => {
    document.getElementById("rule-new-btn").click();
    await tick();
    const form = document.getElementById("rule-new-form");
    form.elements.domain.value = "mounting";
    fire(form.elements.domain, "change");
    await tick();
    form.elements.alias.value = aliasText;
    form.elements.canonical_mounting.value = "THT";
    submit(document, "rule-new-form");
    await tick();
    await tick();
    return form;
  };

  it("posts a rule per comma-separated alias, all onto the one target", async () => {
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: serve(okResp),
    });
    await openWith(document, " przewlekany , THT ,, przewlekany ");

    const posted = fetchMock.mock.calls
      .filter((c) => c[1]?.method === "POST")
      .map((c) => JSON.parse(c[1].body));
    // Blanks dropped, the repeat kept once, every one trimmed and onto "THT".
    expect(posted.map((p) => p.alias)).toEqual(["przewlekany", "THT"]);
    expect(posted.every((p) => p.canonical === "THT" && p.domain === "mounting")).toBe(true);
    expect(document.getElementById("rule-new-dialog").close).toHaveBeenCalled();
  });

  it("stops at a refused alias and leaves only what still has to go in", async () => {
    // The second alias is already used elsewhere; the first has already landed by
    // then, so the field must not offer to create it a second time.
    const { document } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: serve((body) =>
        body.alias === "THT"
          ? Promise.resolve({
              ok: false,
              json: async () => ({ detail: "the alias 'THT' already exists in this domain" }),
            })
          : okResp(),
      ),
    });
    const form = await openWith(document, "przewlekany, THT, drutowy");

    const error = document.getElementById("rule-new-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("already exists");
    // Still open, holding the refused alias and the ones never tried — resubmitting
    // retries exactly the remainder.
    expect(document.getElementById("rule-new-dialog").close).not.toHaveBeenCalled();
    expect(form.elements.alias.value).toBe("THT, drutowy");
  });
});

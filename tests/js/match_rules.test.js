import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, matchRulesPageFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "match_rules.js"];

// A minimal Tabulator cell double: the value being edited + its row's data, plus a
// spy for the revert the code calls when the server rejects an edit.
function editCell(value, row = { id: 7 }) {
  return {
    getValue: () => value,
    getRow: () => ({ getData: () => row }),
    restoreOldValue: vi.fn(),
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
    // package → free text too: a case name ("SOT-23") is whatever the shelf calls it,
    // so there is no list to pick from.
    expect(paramsFor({ domain: "package" })).toEqual({
      values: [],
      autocomplete: true,
      freetext: true,
      listOnEmpty: true,
    });
  });
});

describe("match_rules.js — inline edit", () => {
  it("PATCHes a single field when a cell is edited", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const alias = window.ruleColumns().find((c) => c.field === "alias");
    await alias.cellEdited(editCell("opornik", { id: 42 }));

    const patch = writeCall(fetchMock, "PATCH");
    expect(patch[0]).toBe("/api/admin/match-rules/42");
    expect(patch[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(patch[1].body)).toEqual({ alias: "opornik" });
  });

  it("sends the order as a number", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);
    const order = window.ruleColumns().find((c) => c.field === "sort_order");
    await order.cellEdited(editCell("5", { id: 3 }));
    expect(JSON.parse(writeCall(fetchMock, "PATCH")[1].body)).toEqual({
      sort_order: 5,
    });
  });

  it("reverts the cell and alerts when the server rejects the edit", async () => {
    // The duplicate-alias guard the admin asked for surfaces here.
    const fetchImpl = () =>
      Promise.resolve({
        ok: false,
        json: async () => ({ detail: "an identical rule already exists" }),
      });
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS, { fetchImpl });
    window.alert = vi.fn();
    const cell = editCell("smd", { id: 9 });
    const alias = window.ruleColumns().find((c) => c.field === "alias");
    await alias.cellEdited(cell);

    expect(window.alert).toHaveBeenCalledWith("an identical rule already exists");
    expect(cell.restoreOldValue).toHaveBeenCalled();
  });
});

describe("match_rules.js — loading & delete", () => {
  it("fetches the feed and sets the data", async () => {
    const feed = {
      data: [{ id: 1, domain: "type", alias: "rezystor", canonical: "resistor" }],
    };
    const fetchImpl = () => Promise.resolve({ ok: true, json: async () => feed });
    const { window } = loadPage(matchRulesPageFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.spyOn(window.Tabulator.prototype, "setData");

    await window.loadRules();
    await tick();

    expect(setData).toHaveBeenCalledWith(feed.data);
  });

  it("does not let an unreadable rules feed look like an empty vocabulary", async () => {
    // The match rules ARE the import engine's vocabulary. An empty table says "the
    // engine knows nothing", which is a claim worth being wrong about only when it
    // is true — and a 500 body parses as JSON perfectly well, handing `undefined`
    // to setData with no error of its own.
    const placeholderAfter = async (body, status = 200) => {
      const page = loadPage(matchRulesPageFixture(), SCRIPTS, {
        fetchImpl: () => Promise.resolve({ ok: status === 200, status, json: async () => body }),
      });
      page.window.alert = vi.fn();
      await page.window.loadRules();
      return {
        placeholder: page.window.Tabulator.instances[0].options.placeholder,
        alerts: page.window.alert.mock.calls.length,
      };
    };

    expect(await placeholderAfter({ data: [] })).toEqual({
      placeholder: "No match rules",
      alerts: 0,
    });
    // A 500 that parses: the shape check is the only thing standing between this
    // and a silently blank vocabulary.
    expect(await placeholderAfter({ detail: "Server Error" }, 500)).toEqual({
      placeholder: "Could not load match rules",
      alerts: 1,
    });
  });

  it("frames the table even when it rejects its data", async () => {
    // frameTable is what stops the page scrolling; a table that fails to take its
    // rows must not also be left at its default height.
    const page = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({ data: [] }) }),
    });
    const framed = [];
    page.window.frameTable = (t) => framed.push(t);
    page.window.Tabulator.prototype.setData = () => Promise.reject(new Error("boom"));

    await expect(page.window.loadRules()).rejects.toThrow("boom");

    expect(framed.length).toBe(1);
  });

  it("deletes only after confirmation", async () => {
    const { window, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS);

    window.confirm = vi.fn(() => false);
    window.deleteRule({ id: 5, alias: "x", canonical: "ic" });
    await tick();
    // Declining the confirm sends no DELETE (the startup /api/types fetch aside).
    expect(writeCall(fetchMock, "DELETE")).toBeUndefined();

    window.confirm = vi.fn(() => true);
    window.deleteRule({ id: 5, alias: "x", canonical: "ic" });
    await tick();
    const del = writeCall(fetchMock, "DELETE");
    expect(del[0]).toBe("/api/admin/match-rules/5");
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

  it("takes a free-text target for a package rule, with no parameter scope", async () => {
    const { document, fetchMock } = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: withTypes([{ id: 1, name: "resistor" }]),
    });
    document.getElementById("rule-new-btn").click();
    await tick();
    const form = document.getElementById("rule-new-form");
    form.elements.domain.value = "package";
    fire(form.elements.domain, "change");
    await tick();

    // A package is global (no parameter to scope it to) and its target is free text —
    // the placeholder says so, since the same input also serves param_name.
    expect(document.getElementById("rule-scope-type").hidden).toBe(true);
    expect(document.getElementById("rule-scope-param").hidden).toBe(true);
    expect(form.elements.canonical_text.hidden).toBe(false);
    expect(form.elements.canonical_type.hidden).toBe(true);
    expect(form.elements.canonical_mounting.hidden).toBe(true);
    expect(form.elements.canonical_text.placeholder).toMatch(/SOT-23/);

    form.elements.alias.value = "  obudowa SOT23  ";
    form.elements.canonical_text.value = "SOT-23";
    submit(document, "rule-new-form");
    await tick();

    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/admin/match-rules");
    expect(JSON.parse(post[1].body)).toEqual({
      domain: "package",
      alias: "obudowa SOT23", // trimmed
      canonical: "SOT-23",
      sort_order: 0,
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

describe("match_rules.js — manufacturer aliases", () => {
  // The screen that opens a one-way door: until it existed an alias recorded by a
  // mis-click could not be undone from the app at all, not even by editing the
  // component, because that write path canonicalises too.
  const ALIAS = { id: 3, alias: "MICROCHIP", canonical: "Microchip Technology" };

  function aliasPage(rows = [ALIAS], extra = {}) {
    return loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: (url, opts) => {
        if (url === "/web/api/manufacturer-aliases") {
          return Promise.resolve({ ok: true, json: async () => ({ data: rows }) });
        }
        if (url.startsWith("/web/api/match-rules")) {
          return Promise.resolve({ ok: true, json: async () => ({ data: [] }) });
        }
        return Promise.resolve({ ok: true, json: async () => [], ...extra });
      },
    });
  }

  it("shows the spelling and the name it resolves to", async () => {
    const page = aliasPage();
    // The stub records handlers on the CLASS, so the second table's tableBuilt
    // replaces the first's and neither fires by itself — drive the load directly,
    // as the rules tests do.
    await page.window.loadAliases();
    const columns = page.window.Tabulator.columns.map((c) => c.field);
    expect(columns).toEqual(["alias", "canonical", "actions"]);
    expect(page.window.Tabulator.rows).toEqual([ALIAS]);
  });

  it("distinguishes an empty list from a list it could not read", async () => {
    // The table's placeholder is the ONE empty state — the note above the table
    // already explains where aliases come from, so this only has to say WHICH
    // nothing is on screen. Getting that wrong is how a failed load ends up
    // asserting "you have no aliases", which is the claim the loader refuses.
    const placeholderAfter = async (fetchImpl) => {
      const page = loadPage(matchRulesPageFixture(), SCRIPTS, { fetchImpl });
      page.window.alert = vi.fn();
      await page.window.loadAliases();
      return page.window.Tabulator.instances.at(-1).options.placeholder;
    };
    const feed = (body) => () => Promise.resolve({ ok: true, json: async () => body });

    expect(await placeholderAfter(feed({ data: [] }))).toBe("No manufacturer aliases yet");
    expect(await placeholderAfter(feed({ detail: "Not Found" }))).toBe(
      "Could not load manufacturer aliases",
    );
  });

  it("gives each Forget button the name of the row it acts on", async () => {
    // Every one of them renders the bare word "Forget". Without the row's spelling
    // in the accessible name, a screen-reader user hears a column of identical
    // buttons and cannot tell which alias they are about to drop.
    const page = aliasPage();
    await page.window.loadAliases();
    const actions = page.window.Tabulator.columns.find((c) => c.field === "actions");
    const html = actions.formatter({ getRow: () => ({ getData: () => ALIAS }) });

    expect(html).toContain('aria-label="Forget “MICROCHIP”"');

    // Escaped, because a manufacturer name is whatever a shop or an invoice said it
    // was. A quote in it would otherwise close the attribute and start a new one.
    const quoted = { ...ALIAS, alias: '"><img src=x onerror=alert(1)>' };
    const evil = actions.formatter({ getRow: () => ({ getData: () => quoted }) });
    expect(evil).not.toContain("<img");
  });

  it("frames the rules table even when the alias table rejects its data", async () => {
    // loadAliases is what re-frames the RULES table, because only one table on a
    // page can fill the viewport and that one is the long one. Coupling the two
    // that way is deliberate; leaving the rules table unsized when this one
    // stumbles would not be.
    const page = aliasPage();
    const framed = [];
    page.window.frameTable = (t) => framed.push(t);
    page.window.Tabulator.prototype.setData = () => Promise.reject(new Error("boom"));

    await expect(page.window.loadAliases()).rejects.toThrow("boom");

    expect(framed.length).toBe(1);
  });

  it("forgets one, and reloads rather than guessing what is left", async () => {
    const page = aliasPage();
    await page.window.loadAliases();
    page.window.confirm = vi.fn(() => true);
    page.window.forgetAlias(ALIAS);
    await tick();

    const del = writeCall(page.fetchMock, "DELETE");
    expect(del[0]).toBe("/api/manufacturers/aliases/3");
    expect(del[1].headers["X-CSRF-Token"]).toBe(CSRF);
    // Re-read after the write: the table must show what the server has, not what
    // the client assumed it would have.
    const reloads = page.fetchMock.mock.calls.filter(
      (c) => c[0] === "/web/api/manufacturer-aliases",
    );
    expect(reloads.length).toBe(2);
  });

  it("says what forgetting does and does not touch, and obeys a no", async () => {
    const page = aliasPage();
    await page.window.loadAliases();
    page.window.confirm = vi.fn(() => false);
    page.window.forgetAlias(ALIAS);
    await tick();

    const asked = page.window.confirm.mock.calls[0][0];
    expect(asked).toContain("MICROCHIP");
    expect(asked).toContain("Microchip Technology");
    // "Delete" beside a manufacturer's name reads as though it might rename or
    // remove the parts filed under it. It does neither, and has to say so.
    expect(asked).toContain("keep it");
    expect(writeCall(page.fetchMock, "DELETE")).toBeUndefined();
  });
});

describe("match_rules.js — aliases that fail to load", () => {
  it("says so, and leaves an empty table rather than a broken one", async () => {
    // An HTTP failure still parses as JSON, so reading .data off it yields
    // undefined — which used to reach Tabulator as no rows AND throw on the
    // length check, half-loading the page with no clue why.
    const page = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        url === "/web/api/manufacturer-aliases"
          ? Promise.resolve({
              ok: false,
              status: 404,
              json: async () => ({ detail: "Not Found" }),
            })
          : Promise.resolve({ ok: true, json: async () => ({ data: [] }) }),
    });
    page.window.alert = vi.fn();

    await page.window.loadAliases(); // must not throw

    expect(page.window.alert).toHaveBeenCalledTimes(1);
    expect(page.window.Tabulator.rows).toEqual([]);
  });

  it("treats a 200 of the wrong shape as a failure, not as an empty list", async () => {
    // Silently showing an empty table would answer "you have no aliases", which is
    // a worse thing to be wrong about than "could not load".
    const page = loadPage(matchRulesPageFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        Promise.resolve({
          ok: true,
          json: async () => (url === "/web/api/manufacturer-aliases" ? {} : { data: [] }),
        }),
    });
    page.window.alert = vi.fn();

    await page.window.loadAliases();

    expect(page.window.alert).toHaveBeenCalledTimes(1);
  });
});

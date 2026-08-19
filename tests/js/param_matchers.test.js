import { describe, it, expect } from "vitest";
import { loadPage, tick, typesAdminPageFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "param_matchers.js"];

// An enum parameter (Dielectric, values C0G/X7R) and a plain number one.
const ENUM_PARAM = {
  id: 9,
  name: "dielectric",
  label: "Dielectric",
  data_type: "enum",
  enum_values: ["C0G", "X7R"],
};
const NUMBER_PARAM = {
  id: 4,
  name: "resistance",
  label: "Resistance",
  data_type: "number",
  enum_values: [],
};

// The admin rules feed: one value rule + one name rule scoped to param 9, one for
// another parameter (must be excluded).
function feed() {
  return {
    data: [
      { id: 1, domain: "enum_value", alias: "NP0", canonical: "C0G", parameter_definition_id: 9, enum_values: ["C0G", "X7R"], sort_order: 0 },
      { id: 2, domain: "param_name", alias: "Dielektryk", canonical: "dielectric", parameter_definition_id: 9, enum_values: [], sort_order: 0 },
      { id: 3, domain: "enum_value", alias: "elsewhere", canonical: "foo", parameter_definition_id: 99, enum_values: [], sort_order: 0 },
    ],
  };
}

// A fetch double backed by a real store: the panel reloads the lists after every write
// now (a row is a target and stands for several rules), so a feed that ignored the
// writes would make every assertion about what the lists then show vacuous.
function makeFetch(overrides = {}) {
  let nextId = 100;
  const rows = overrides.rows ? overrides.rows.map((r) => ({ ...r })) : feed().data;
  const ok = (body) => Promise.resolve({ ok: true, json: async () => body });
  return (url, opts) => {
    if (url === "/web/api/match-rules") return ok({ data: rows.map((r) => ({ ...r })) });
    if (url.startsWith("/api/admin/match-rules")) {
      if (overrides.writeFails) {
        return Promise.resolve({ ok: false, status: 422, json: async () => ({ detail: "duplicate alias" }) });
      }
      const body = opts?.body ? JSON.parse(opts.body) : {};
      if (opts?.method === "POST") {
        const row = { enum_values: [], sort_order: 0, ...body, id: nextId++ };
        rows.push(row);
        return ok(row);
      }
      const at = rows.findIndex((r) => r.id === Number(url.split("/").pop()));
      if (opts?.method === "PATCH") {
        Object.assign(rows[at], body);
        return ok(rows[at]);
      }
      if (opts?.method === "DELETE") {
        rows.splice(at, 1);
        return ok({});
      }
    }
    return ok({});
  };
}

const aliasFields = (rows) => rows.map((li) => li.querySelector(".pm-alias").value);

const valueRows = (d) => [...d.querySelectorAll("#pm-value-list li")];
const nameRows = (d) => [...d.querySelectorAll("#pm-name-list li")];
const writeCall = (fetchMock, method) =>
  fetchMock.mock.calls.find((c) => c[1]?.method === method);

describe("param_matchers.js", () => {
  it("splits a parameter's rules into value and name sections (others excluded)", async () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();
    expect(valueRows(document).length).toBe(1); // rule 1 (NP0 → C0G)
    expect(nameRows(document).length).toBe(1); // rule 2 (Dielektryk)
    expect(document.getElementById("param-matchers-title").textContent).toBe("Dielectric");
    expect(document.getElementById("pm-value-section").hidden).toBe(false);
  });

  it("rapid-adds a value alias: POSTs enum_value with the chosen value, then clears", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();
    const before = valueRows(document).length;

    document.getElementById("pm-value-target").value = "X7R";
    document.getElementById("pm-value-alias").value = "COG-ish";
    document.getElementById("pm-value-add").click();
    await tick();

    expect(JSON.parse(writeCall(fetchMock, "POST")[1].body)).toEqual({
      domain: "enum_value",
      alias: "COG-ish",
      canonical: "X7R",
      parameter_definition_id: 9,
      sort_order: 0,
    });
    expect(valueRows(document).length).toBe(before + 1);
    expect(document.getElementById("pm-value-alias").value).toBe(""); // cleared
  });

  it("rapid-adds a name alias: POSTs param_name with the parameter's own name", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();
    const before = nameRows(document).length;

    document.getElementById("pm-name-alias").value = "Dielectric material";
    document.getElementById("pm-name-add").click();
    await tick();

    expect(JSON.parse(writeCall(fetchMock, "POST")[1].body)).toMatchObject({
      domain: "param_name",
      canonical: "dielectric",
      parameter_definition_id: 9,
    });
    // Same target as the alias already there, so it JOINS that row rather than
    // starting one of its own — one row per target is the whole point.
    expect(nameRows(document).length).toBe(before);
    expect(aliasFields(nameRows(document))).toEqual([
      "Dielektryk, Dielectric material",
    ]);
  });

  it("hides the value section for a non-enum parameter (name aliases only)", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(NUMBER_PARAM);
    await tick();
    expect(document.getElementById("pm-value-section").hidden).toBe(true);

    document.getElementById("pm-name-alias").value = "Rezystancja";
    document.getElementById("pm-name-add").click();
    await tick();
    expect(JSON.parse(writeCall(fetchMock, "POST")[1].body)).toMatchObject({
      domain: "param_name",
      canonical: "resistance",
      parameter_definition_id: 4,
    });
  });

  it("edits a value rule's alias and target inline (PATCH)", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();

    // Swapping the one word in the list is a rename of its rule, not a delete plus a
    // create. The list reloads after the write, so the row is re-read for the second.
    const alias = valueRows(document)[0].querySelector(".pm-alias");
    alias.value = "NPO";
    alias.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    await tick();
    let patch = writeCall(fetchMock, "PATCH");
    expect(patch[0]).toBe("/api/admin/match-rules/1");
    expect(JSON.parse(patch[1].body)).toEqual({ alias: "NPO" });
    expect(writeCall(fetchMock, "DELETE")).toBeUndefined();

    const select = valueRows(document)[0].querySelector("select");
    select.value = "X7R";
    select.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    await tick();
    patch = fetchMock.mock.calls.filter((c) => c[1]?.method === "PATCH").at(-1);
    expect(JSON.parse(patch[1].body)).toEqual({ canonical: "X7R" });
  });

  it("deletes a rule (DELETE) and drops its row", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();
    nameRows(document)[0].querySelector(".pm-del").click();
    await tick();
    expect(writeCall(fetchMock, "DELETE")[0]).toBe("/api/admin/match-rules/2");
    expect(nameRows(document).length).toBe(0);
  });

  it("keeps the alias on a failed add and shows the error", async () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch({ writeFails: true }),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();
    document.getElementById("pm-value-alias").value = "NP0";
    document.getElementById("pm-value-add").click();
    await tick();
    expect(document.getElementById("pm-value-alias").value).toBe("NP0"); // not cleared
    expect(document.getElementById("param-matchers-error").hidden).toBe(false);
    // A failed write must NOT claim it saved.
    expect(document.getElementById("param-matchers-status").textContent).not.toMatch(/Saved/);
  });

  it("confirms each successful write with a 'Saved' status (autosave)", async () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM);
    await tick();
    // On open, the panel states the save model up front.
    expect(document.getElementById("param-matchers-status").textContent).toMatch(/automatically/i);

    document.getElementById("pm-name-alias").value = "Dielectric material";
    document.getElementById("pm-name-add").click();
    await tick();
    expect(document.getElementById("param-matchers-status").textContent).toMatch(/Saved/);
  });
});

describe("param_matchers.js — one row per target", () => {
  // Two spellings of the same value, one of the other, two name aliases.
  const shared = [
    { id: 1, domain: "enum_value", alias: "NP0", canonical: "C0G", parameter_definition_id: 9, enum_values: ["C0G", "X7R"], sort_order: 0 },
    { id: 2, domain: "enum_value", alias: "COG", canonical: "C0G", parameter_definition_id: 9, enum_values: ["C0G", "X7R"], sort_order: 3 },
    { id: 3, domain: "enum_value", alias: "X7R-ish", canonical: "X7R", parameter_definition_id: 9, enum_values: ["C0G", "X7R"], sort_order: 0 },
    { id: 4, domain: "param_name", alias: "Dielektryk", canonical: "dielectric", parameter_definition_id: 9, enum_values: [], sort_order: 0 },
  ];
  const open = async (overrides = { rows: shared }) => {
    const page = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(overrides),
    });
    await page.window.openParamMatchers(ENUM_PARAM);
    await tick();
    return page;
  };

  it("shows a value's aliases as one comma-separated row", async () => {
    const { document } = await open();
    // Three value rules, two values — so two rows, not three.
    expect(aliasFields(valueRows(document))).toEqual(["NP0, COG", "X7R-ish"]);
    expect(aliasFields(nameRows(document))).toEqual(["Dielektryk"]);
  });

  it("rapid-adds a comma-separated run onto one value", async () => {
    const { document, fetchMock } = await open();
    document.getElementById("pm-value-target").value = "X7R";
    document.getElementById("pm-value-alias").value = " X7R , ,7R, X7R ";
    document.getElementById("pm-value-add").click();
    await tick();
    await tick();

    // A rule each, blanks dropped and the repeat kept once — all onto X7R, so they
    // join the row that value already had.
    expect(
      fetchMock.mock.calls
        .filter((c) => c[1]?.method === "POST")
        .map((c) => JSON.parse(c[1].body).alias),
    ).toEqual(["X7R", "7R"]);
    expect(aliasFields(valueRows(document))).toEqual([
      "NP0, COG",
      "X7R-ish, X7R, 7R",
    ]);
    expect(document.getElementById("pm-value-alias").value).toBe("");
  });

  it("drops a word from the list by deleting just that rule", async () => {
    const { document, fetchMock } = await open();
    const field = valueRows(document)[0].querySelector(".pm-alias");
    field.value = "NP0";
    field.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    await tick();

    expect(writeCall(fetchMock, "DELETE")[0]).toBe("/api/admin/match-rules/2");
    expect(aliasFields(valueRows(document))).toEqual(["NP0", "X7R-ish"]);
  });

  it("refuses to empty the field, which would delete the whole target", async () => {
    const { document, fetchMock } = await open();
    const field = valueRows(document)[0].querySelector(".pm-alias");
    field.value = "  ,  ";
    field.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();

    // Nothing written, the text put back, and ✕ named as the way out.
    expect(writeCall(fetchMock, "DELETE")).toBeUndefined();
    expect(field.value).toBe("NP0, COG");
    expect(document.getElementById("param-matchers-error").hidden).toBe(false);
  });

  it("deletes every rule behind the row, not just the first", async () => {
    const { document, fetchMock } = await open();
    valueRows(document)[0].querySelector(".pm-del").click();
    await tick();
    await tick();

    expect(
      fetchMock.mock.calls.filter((c) => c[1]?.method === "DELETE").map((c) => c[0]),
    ).toEqual(["/api/admin/match-rules/1", "/api/admin/match-rules/2"]);
    expect(aliasFields(valueRows(document))).toEqual(["X7R-ish"]);
  });

  it("retargets every alias at once, merging into the row that value already has", async () => {
    const { document, fetchMock } = await open();
    // The C0G row, deliberately — it stands for TWO rules, so a retarget that moved
    // only the first would leave "NP0" behind on C0G and split the row in half.
    const select = valueRows(document)[0].querySelector("select");
    select.value = "X7R";
    select.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    await tick();

    const patches = fetchMock.mock.calls.filter((c) => c[1]?.method === "PATCH");
    expect(patches.map((c) => c[0])).toEqual([
      "/api/admin/match-rules/1",
      "/api/admin/match-rules/2",
    ]);
    expect(JSON.parse(patches[0][1].body)).toEqual({ canonical: "X7R" });
    // Both land on the value that already had an alias, so it is one row now — in
    // feed order, which is by rule id.
    expect(aliasFields(valueRows(document))).toEqual(["NP0, COG, X7R-ish"]);
  });
});

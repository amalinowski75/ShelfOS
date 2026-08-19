import { describe, it, expect, vi } from "vitest";
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

// The admin rules feed: a couple scoped to param 9, one to another param.
function feed() {
  return {
    data: [
      { id: 1, domain: "enum_value", alias: "NP0", canonical: "C0G", parameter_definition_id: 9, enum_values: ["C0G", "X7R"], sort_order: 0 },
      { id: 2, domain: "param_name", alias: "Dielektryk", canonical: "dielectric", parameter_definition_id: 9, enum_values: [], sort_order: 0 },
      { id: 3, domain: "enum_value", alias: "elsewhere", canonical: "foo", parameter_definition_id: 99, enum_values: [], sort_order: 0 },
    ],
  };
}

// A fetch double: the feed on GET, and ok+echo for the admin writes (POST returns the
// body with an id so the panel can append the new row).
function makeFetch(overrides = {}) {
  let nextId = 100;
  return (url, opts) => {
    if (url === "/web/api/match-rules") {
      return Promise.resolve({ ok: true, json: async () => feed() });
    }
    if (url.startsWith("/api/admin/match-rules")) {
      if (overrides.writeFails) {
        return Promise.resolve({ ok: false, status: 422, json: async () => ({ detail: "duplicate alias" }) });
      }
      const body = opts?.body ? JSON.parse(opts.body) : {};
      return Promise.resolve({ ok: true, json: async () => ({ id: nextId++, ...body }) });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  };
}

const rows = (document) => [...document.querySelectorAll("#param-matchers-list li")];
const writeCall = (fetchMock, method) =>
  fetchMock.mock.calls.find((c) => c[1]?.method === method);

describe("param_matchers.js", () => {
  it("lists only the rules scoped to this parameter", async () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM, 3);
    await tick();
    // Two of the three feed rows belong to param 9; the param-99 one is excluded.
    expect(rows(document).length).toBe(2);
    expect(document.getElementById("param-matchers-title").textContent).toBe("Dielectric");
  });

  it("rapid-adds an enum_value rule: POSTs the value + param id, then clears", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM, 3);
    await tick();
    const before = rows(document).length;

    const target = document.getElementById("pm-add-target");
    target.value = "X7R"; // an enum value → an enum_value rule
    document.getElementById("pm-add-alias").value = "COG-ish";
    document.getElementById("pm-add-btn").click();
    await tick();

    const post = writeCall(fetchMock, "POST");
    expect(JSON.parse(post[1].body)).toEqual({
      domain: "enum_value",
      alias: "COG-ish",
      canonical: "X7R",
      parameter_definition_id: 9,
      sort_order: 0,
    });
    // Appended, and the alias cleared for the next one (no modal, no reload).
    expect(rows(document).length).toBe(before + 1);
    expect(document.getElementById("pm-add-alias").value).toBe("");
  });

  it("adds a param_name rule via the 'as parameter name' option", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM, 3);
    await tick();

    const target = document.getElementById("pm-add-target");
    target.selectedIndex = target.options.length - 1; // the param_name option (last)
    document.getElementById("pm-add-alias").value = "Dielectric material";
    document.getElementById("pm-add-btn").click();
    await tick();

    expect(JSON.parse(writeCall(fetchMock, "POST")[1].body)).toMatchObject({
      domain: "param_name",
      canonical: "dielectric", // the parameter's own name
      parameter_definition_id: 9,
    });
  });

  it("for a non-enum parameter, hides the value picker and adds a param_name rule", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(NUMBER_PARAM, 3);
    await tick();
    expect(document.getElementById("pm-add-target").hidden).toBe(true);

    document.getElementById("pm-add-alias").value = "Rezystancja";
    document.getElementById("pm-add-btn").click();
    await tick();
    expect(JSON.parse(writeCall(fetchMock, "POST")[1].body)).toMatchObject({
      domain: "param_name",
      canonical: "resistance",
      parameter_definition_id: 4,
    });
  });

  it("edits an alias inline (PATCH) and a value inline (PATCH)", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM, 3);
    await tick();

    // First row is rule 1 (enum_value NP0 → C0G): rename its alias.
    const first = rows(document)[0];
    const alias = first.querySelector("input");
    alias.value = "NPO";
    alias.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    let patch = writeCall(fetchMock, "PATCH");
    expect(patch[0]).toBe("/api/admin/match-rules/1");
    expect(JSON.parse(patch[1].body)).toEqual({ alias: "NPO" });

    // …and change its target value.
    const select = first.querySelector("select");
    select.value = "X7R";
    select.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    patch = fetchMock.mock.calls.filter((c) => c[1]?.method === "PATCH").at(-1);
    expect(JSON.parse(patch[1].body)).toEqual({ canonical: "X7R" });
  });

  it("deletes a rule (DELETE) and drops its row", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch(),
    });
    await window.openParamMatchers(ENUM_PARAM, 3);
    await tick();
    const before = rows(document).length;
    rows(document)[0].querySelector(".pm-del").click();
    await tick();
    expect(writeCall(fetchMock, "DELETE")[0]).toBe("/api/admin/match-rules/1");
    expect(rows(document).length).toBe(before - 1);
  });

  it("keeps the alias on a failed add and shows the error", async () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: makeFetch({ writeFails: true }),
    });
    await window.openParamMatchers(ENUM_PARAM, 3);
    await tick();
    document.getElementById("pm-add-alias").value = "NP0";
    document.getElementById("pm-add-btn").click();
    await tick();
    expect(document.getElementById("pm-add-alias").value).toBe("NP0"); // not cleared
    expect(document.getElementById("param-matchers-error").hidden).toBe(false);
  });
});

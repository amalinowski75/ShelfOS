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
    expect(nameRows(document).length).toBe(before + 1);
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

    const row = valueRows(document)[0]; // rule 1 (NP0 → C0G)
    const alias = row.querySelector("input");
    alias.value = "NPO";
    alias.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();
    let patch = writeCall(fetchMock, "PATCH");
    expect(patch[0]).toBe("/api/admin/match-rules/1");
    expect(JSON.parse(patch[1].body)).toEqual({ alias: "NPO" });

    const select = row.querySelector("select");
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
  });
});

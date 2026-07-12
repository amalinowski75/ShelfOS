import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF, componentPageFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "app.js"];

const DEFS = [
  { id: 10, label: "Resistance", data_type: "number", unit: "Ω", enum_values: [] },
  { id: 11, label: "Package", data_type: "text", unit: null, enum_values: [] },
  { id: 12, label: "RoHS", data_type: "bool", unit: null, enum_values: [] },
  {
    id: 13,
    label: "Dielectric",
    data_type: "enum",
    unit: null,
    enum_values: ["X7R", "C0G"],
  },
];

// Routes the endpoints the New Component flow touches.
function fetchImpl(url, opts) {
  if (url.startsWith("/api/types/") && url.endsWith("/parameters")) {
    return Promise.resolve({ ok: true, json: async () => DEFS });
  }
  if (url.startsWith("/web/api/components")) {
    return Promise.resolve({
      ok: true,
      json: async () => ({ columns: [], data: [] }),
    });
  }
  if (url === "/api/components" && opts?.method === "POST") {
    return Promise.resolve({ ok: true, json: async () => ({ id: 99, type_id: 1 }) });
  }
  return Promise.resolve({ ok: true, json: async () => ({}) });
}

function fire(el, type) {
  el.dispatchEvent(
    new el.ownerDocument.defaultView.Event(type, { cancelable: true, bubbles: true }),
  );
}

// Open the dialog and pick a type so its parameter fields render.
async function openWithType(impl = fetchImpl) {
  const handles = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
  handles.document.getElementById("new-component-btn").click();
  const typeSelect = handles.document.getElementById("component-type");
  typeSelect.value = "1";
  fire(typeSelect, "change");
  await tick(); // let the effective-parameters fetch resolve
  return handles;
}

describe("app.js — new component", () => {
  it("renders one value field per effective parameter", async () => {
    const { document } = await openWithType();
    const inputs = document.querySelectorAll(
      "#component-params [data-definition-id]",
    );
    expect(inputs.length).toBe(4);
    // enum -> select with the allowed tokens (plus a blank); bool -> yes/no.
    const enumSelect = document.querySelector('[data-definition-id="13"]');
    expect([...enumSelect.options].map((o) => o.value)).toEqual(["", "X7R", "C0G"]);
    const boolSelect = document.querySelector('[data-definition-id="12"]');
    expect([...boolSelect.options].map((o) => o.value)).toEqual(["", "true", "false"]);
  });

  it("posts only filled parameters: bool as boolean, number as its raw string", async () => {
    const { document, fetchMock } = await openWithType();
    document.getElementById("component-form").mpn.value = "R-100";
    document.querySelector('[data-definition-id="10"]').value = "4k7"; // number
    document.querySelector('[data-definition-id="12"]').value = "true"; // bool
    // Leave the text (11) and enum (13) fields empty — they must be skipped.
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components" && opts.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(post[1].headers["X-CSRF-Token"]).toBe(CSRF);
    const payload = JSON.parse(post[1].body);
    expect(payload.type_id).toBe(1);
    expect(payload.mpn).toBe("R-100");
    expect(payload.manufacturer).toBeNull();
    expect(payload.parameters).toEqual([
      { parameter_definition_id: 10, value: "4k7" },
      { parameter_definition_id: 12, value: true },
    ]);
  });

  it("surfaces the server error when create fails", async () => {
    const failImpl = (url, opts) =>
      url === "/api/components" && opts?.method === "POST"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "duplicate" }) })
        : fetchImpl(url, opts);
    const { document } = await openWithType(failImpl);
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const error = document.getElementById("component-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("duplicate");
  });

  it("keeps explicit falsy values (bool 'no', number '0')", async () => {
    const { document, fetchMock } = await openWithType();
    document.querySelector('[data-definition-id="10"]').value = "0"; // number zero
    document.querySelector('[data-definition-id="12"]').value = "false"; // bool no
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components" && opts.method === "POST",
    );
    expect(JSON.parse(post[1].body).parameters).toEqual([
      { parameter_definition_id: 10, value: "0" },
      { parameter_definition_id: 12, value: false },
    ]);
  });

  it("ignores a stale slow response for a superseded type", async () => {
    const A = { id: 20, label: "A", data_type: "text", unit: null, enum_values: [] };
    const B = { id: 21, label: "B", data_type: "text", unit: null, enum_values: [] };
    const raced = (url) => {
      if (url === "/api/types/1/parameters") {
        return new Promise((resolve) =>
          setTimeout(() => resolve({ ok: true, json: async () => [A] }), 30),
        );
      }
      if (url === "/api/types/2/parameters") {
        return Promise.resolve({ ok: true, json: async () => [B] });
      }
      return fetchImpl(url);
    };
    const { document } = loadPage(
      componentPageFixture([
        { id: 1, name: "r" },
        { id: 2, name: "c" },
      ]),
      SCRIPTS,
      { fetchImpl: raced },
    );
    document.getElementById("new-component-btn").click();
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "1";
    fire(typeSelect, "change"); // slow
    typeSelect.value = "2";
    fire(typeSelect, "change"); // fast — supersedes the first
    await new Promise((resolve) => setTimeout(resolve, 60)); // let both settle

    const ids = [
      ...document.querySelectorAll("#component-params [data-definition-id]"),
    ].map((i) => i.dataset.definitionId);
    expect(ids).toEqual(["21"]); // only the newer type's field, not the stale one
  });

  it("clears the parameter fields when the dialog is reopened", async () => {
    const { document } = await openWithType();
    expect(
      document.querySelectorAll("#component-params [data-definition-id]").length,
    ).toBe(4);

    document.getElementById("new-component-btn").click(); // reopen
    expect(
      document.querySelectorAll("#component-params [data-definition-id]").length,
    ).toBe(0);
    expect(document.getElementById("component-type").value).toBe("");
  });

  it("loads cleanly when the create controls are absent (read-only)", () => {
    // A page without #component-dialog / #new-component-btn must not throw at
    // load; the harness fails the test on any unhandled script error.
    const { document } = loadPage(
      `<select id="type-filter"></select>
       <div id="components-table"></div>
       <dialog id="stock-dialog"><form id="stock-form"></form></dialog>`,
      SCRIPTS,
    );
    expect(document.getElementById("new-component-btn")).toBeNull();
  });

  it("surfaces a message when the create request never reaches the server", async () => {
    const failImpl = (url, opts) =>
      url === "/api/components" && opts?.method === "POST"
        ? Promise.reject(new Error("network down"))
        : fetchImpl(url, opts);
    const { document } = await openWithType(failImpl);
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const error = document.getElementById("component-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/Could not reach the server/);
  });

  it("shows a loading placeholder while a type's parameters are fetched", () => {
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("new-component-btn").click();
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "1";
    fire(typeSelect, "change");
    // Synchronously, before the fetch resolves, the hint reads "Loading…".
    const hint = document.getElementById("component-params-hint");
    expect(hint.hidden).toBe(false);
    expect(hint.textContent).toBe("Loading…");
  });
});

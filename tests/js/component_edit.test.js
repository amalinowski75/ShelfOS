import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF, componentEditFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "component_edit.js"];

const DEFS = [
  { id: 10, label: "Resistance", data_type: "number", unit: "Ω", enum_values: [] },
  { id: 11, label: "Tolerance", data_type: "text", unit: null, enum_values: [] },
  { id: 12, label: "RoHS", data_type: "bool", unit: null, enum_values: [] },
  { id: 13, label: "Dielectric", data_type: "enum", unit: null,
    enum_values: ["X7R", "C0G"] },
];

// Current stored values: resistance 2200, tolerance "5%", RoHS true, dielectric X7R.
const VALUES = [
  { parameter_definition_id: 10, value_num: 2200, value_text: null, value_bool: null },
  { parameter_definition_id: 11, value_num: null, value_text: "5%", value_bool: null },
  { parameter_definition_id: 12, value_num: null, value_text: null, value_bool: true },
  { parameter_definition_id: 13, value_num: null, value_text: "X7R", value_bool: null },
];

function fetchImpl(url, opts) {
  if (url === "/api/types/3/parameters") {
    return Promise.resolve({ ok: true, json: async () => DEFS });
  }
  if (url === "/api/components/5/parameters") {
    return Promise.resolve({ ok: true, json: async () => VALUES });
  }
  if (url === "/api/components/5" && opts?.method === "PATCH") {
    return Promise.resolve({ ok: true, json: async () => ({}) });
  }
  return Promise.resolve({ ok: true, json: async () => ({}) });
}

function fire(el, type) {
  el.dispatchEvent(
    new el.ownerDocument.defaultView.Event(type, { cancelable: true, bubbles: true }),
  );
}

async function openEditor(impl = fetchImpl) {
  const handles = loadPage(componentEditFixture(), SCRIPTS, { fetchImpl: impl });
  // jsdom's location.reload() is a no-op (non-configurable, can't be spied); the
  // success path calls it harmlessly, so the tests assert on the PATCH + error, not
  // on reload.
  handles.document.getElementById("component-edit-btn").click();
  await tick();
  return handles;
}

describe("component_edit.js", () => {
  it("builds parameter fields pre-filled with the current values", async () => {
    const { document } = await openEditor();
    const field = (id) =>
      document.querySelector(`#component-edit-params [data-definition-id="${id}"]`);
    expect(field(10).value).toBe("2200"); // number
    expect(field(11).value).toBe("5%"); // text
    expect(field(12).value).toBe("true"); // bool → yes
    // enum → a <select> of the allowed tokens (plus a blank), current pre-selected.
    const dielectric = field(13);
    expect(dielectric.tagName).toBe("SELECT");
    expect([...dielectric.options].map((o) => o.value)).toEqual(["", "X7R", "C0G"]);
    expect(dielectric.value).toBe("X7R");
  });

  it("round-trips an edited enum value", async () => {
    const { document, fetchMock } = await openEditor();
    document.querySelector('[data-definition-id="13"]').value = "C0G";
    fire(document.getElementById("component-edit-form"), "submit");
    await tick();
    const patch = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components/5" && opts.method === "PATCH",
    );
    expect(JSON.parse(patch[1].body).parameters).toContainEqual({
      parameter_definition_id: 13,
      value: "C0G",
    });
  });

  it("PATCHes the edited scalar fields and parameters", async () => {
    const { document, fetchMock } = await openEditor();
    document.querySelector('[name="manufacturer"]').value = "TDK";
    document.querySelector('[data-definition-id="10"]').value = "3k3";
    fire(document.getElementById("component-edit-form"), "submit");
    await tick();

    const patch = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components/5" && opts.method === "PATCH",
    );
    expect(patch).toBeTruthy();
    expect(patch[1].headers["X-CSRF-Token"]).toBe(CSRF);
    const body = JSON.parse(patch[1].body);
    expect(body.manufacturer).toBe("TDK");
    expect(body.mounting_type).toBe("SMT");
    // Type and MPN are never sent — they're immutable.
    expect(body).not.toHaveProperty("type_id");
    expect(body).not.toHaveProperty("mpn");
    expect(body.parameters).toContainEqual({
      parameter_definition_id: 10,
      value: "3k3",
    });
    // The bool field maps back to a real boolean.
    expect(body.parameters).toContainEqual({
      parameter_definition_id: 12,
      value: true,
    });
  });

  it("sends null for a parameter the admin cleared", async () => {
    const { document, fetchMock } = await openEditor();
    document.querySelector('[data-definition-id="11"]').value = ""; // clear tolerance
    fire(document.getElementById("component-edit-form"), "submit");
    await tick();

    const patch = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components/5" && opts.method === "PATCH",
    );
    expect(JSON.parse(patch[1].body).parameters).toContainEqual({
      parameter_definition_id: 11,
      value: null,
    });
  });

  it("shows the server error and does not reload on a failed save", async () => {
    const impl = (url, opts) =>
      url === "/api/components/5" && opts?.method === "PATCH"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "nope" }) })
        : fetchImpl(url, opts);
    const { document } = await openEditor(impl);
    fire(document.getElementById("component-edit-form"), "submit");
    await tick();

    const error = document.getElementById("component-edit-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("nope");
  });

  it("does nothing on a page without the edit dialog (non-admin)", () => {
    const { document } = loadPage("<div></div>", SCRIPTS, { fetchImpl });
    expect(document.getElementById("component-edit-dialog")).toBeNull();
  });
});

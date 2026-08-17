import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, typesAdminPageFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "types_admin.js"];

const cell = (value, row = {}) => ({
  getValue: () => value,
  getRow: () => ({ getData: () => row }),
});

function submit(document, formId) {
  document
    .getElementById(formId)
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

const ok = (data) => Promise.resolve({ ok: true, json: async () => data });
const fail = (detail, status = 422) =>
  Promise.resolve({ ok: false, status, json: async () => ({ detail }) });

const RESISTOR = {
  id: 3,
  name: "resistor",
  parent_name: "passive",
  component_count: 2,
  parameters: [
    {
      id: 9,
      name: "dielectric",
      label: "Dielectric",
      data_type: "enum",
      unit: null,
      sort_order: 0,
      is_table_column: true,
      is_filterable: false,
      enum_values: ["C0G", "X7R"],
    },
  ],
};

describe("types_admin.js — columns", () => {
  it("labels columns and formats parent / parameter count", () => {
    const { window } = loadPage(typesAdminPageFixture(), SCRIPTS);
    const columns = window.typeColumns();
    expect(columns.map((c) => c.field)).toEqual([
      "name",
      "parent_name",
      "component_count",
      "parameters",
      "actions",
    ]);
    expect(columns[1].formatter(cell(null))).toContain("—"); // no parent
    expect(columns[1].formatter(cell("passive"))).toBe("passive");
    expect(columns[3].formatter(cell([{}, {}]))).toBe("2"); // count, not the list
  });

  it("routes row-action clicks to rename / params / delete", () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS);
    const actions = window.typeColumns()[4];
    const click = (act) =>
      actions.cellClick(
        { target: { dataset: { act } } },
        { getRow: () => ({ getData: () => RESISTOR }) },
      );

    click("rename");
    expect(document.getElementById("type-rename-form").type_id.value).toBe("3");
    expect(document.getElementById("type-rename-form").elements.name.value).toBe(
      "resistor",
    );

    click("params");
    expect(document.getElementById("type-params-name").textContent).toBe("resistor");
    expect(document.querySelectorAll("#type-params-list li").length).toBe(1);
  });
});

describe("types_admin.js — type writes", () => {
  it("renames a type via PATCH", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openRenameDialog(RESISTOR);
    document.getElementById("type-rename-form").elements.name.value = "res";

    submit(document, "type-rename-form");
    await tick();

    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/types/3");
    expect(call[1].method).toBe("PATCH");
    expect(call[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(call[1].body)).toEqual({ name: "res" });
  });

  it("surfaces the in-use refusal when a delete is blocked", async () => {
    const { window } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: () => fail("2 components use this type"),
    });
    window.alert = vi.fn();

    window.deleteType(RESISTOR);
    await tick();

    expect(window.alert).toHaveBeenCalledWith("2 components use this type");
  });
});

describe("types_admin.js — parameter writes", () => {
  it("edits a parameter, sending enum tokens for an enum def", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamsDialog(RESISTOR);
    // The first row's Edit button (Edit, then Delete).
    document.querySelector("#type-params-list li button").click();

    const form = document.getElementById("param-edit-form");
    expect(form.definition_id.value).toBe("9");
    expect(form.elements.data_type.value).toBe("enum");
    expect(form.querySelector(".param-enum").hidden).toBe(false); // shown for enum
    expect(form.elements.enum_values.value).toBe("C0G, X7R");

    form.elements.label.value = "Dielectric material";
    form.elements.enum_values.value = "C0G, X7R, Y5V";
    submit(document, "param-edit-form");
    await tick();

    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/parameters/9");
    expect(call[1].method).toBe("PATCH");
    const body = JSON.parse(call[1].body);
    expect(body.label).toBe("Dielectric material");
    expect(body.enum_values).toEqual(["C0G", "X7R", "Y5V"]);
  });

  it("deletes a parameter via DELETE", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamsDialog(RESISTOR);
    // The second button in the row is Delete.
    document.querySelectorAll("#type-params-list li button")[1].click();
    await tick();

    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/parameters/9");
    expect(call[1].method).toBe("DELETE");
  });

  it("adds a parameter to the open type via POST", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamsDialog(RESISTOR);
    document.getElementById("type-params-add").click();

    const form = document.getElementById("param-add-form");
    expect(form.type_id.value).toBe("3");
    form.elements.name.value = "tolerance";
    form.elements.label.value = "Tolerance";
    form.elements.data_type.value = "number";
    form.elements.unit.value = "%";
    submit(document, "param-add-form");
    await tick();

    const call = fetchMock.mock.calls.find(
      ([u]) => u === "/api/types/3/parameters",
    );
    expect(call[1].method).toBe("POST");
    const body = JSON.parse(call[1].body);
    expect(body).toMatchObject({
      name: "tolerance",
      label: "Tolerance",
      data_type: "number",
      unit: "%",
    });
    expect(body.enum_values).toBeUndefined(); // not an enum, so no tokens sent
  });
});

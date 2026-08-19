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

const DIELECTRIC = {
  id: 9,
  name: "dielectric",
  label: "Dielectric",
  data_type: "enum",
  unit: null,
  sort_order: 0,
  is_table_column: true,
  is_filterable: false,
  enum_values: ["C0G", "X7R"],
  in_use_count: 0,
  deletable: true,
};

const RESISTOR = {
  id: 3,
  name: "resistor",
  parent_name: "passive",
  component_count: 2,
  child_count: 0,
  deletable: false, // 2 components use it
  parameters: [DIELECTRIC],
};

describe("types_admin.js — columns", () => {
  it("labels columns and formats parent / child / parameter count", () => {
    const { window } = loadPage(typesAdminPageFixture(), SCRIPTS);
    const columns = window.typeColumns();
    expect(columns.map((c) => c.field)).toEqual([
      "name",
      "parent_name",
      "component_count",
      "child_count",
      "parameters",
      "actions",
    ]);
    expect(columns[1].formatter(cell(null))).toContain("—"); // no parent
    expect(columns[1].formatter(cell("passive"))).toBe("passive");
    expect(columns[4].formatter(cell([{}, {}]))).toBe("2"); // count, not the list
  });

  it("offers a live filter on the type name", () => {
    const { window } = loadPage(typesAdminPageFixture(), SCRIPTS);
    expect(window.typeColumns()[0].headerFilter).toBe("input");
  });

  it("routes row-action clicks to rename / params / delete", () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS);
    const actions = window.typeColumns()[5];
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

  it("guards against a double submit", async () => {
    // A never-resolving fetch keeps the first submit in flight; the second must be
    // dropped by makeGuard rather than firing a duplicate request.
    let calls = 0;
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: () => {
        calls += 1;
        return new Promise(() => {}); // never resolves
      },
    });
    window.openRenameDialog(RESISTOR);
    document.getElementById("type-rename-form").elements.name.value = "res";
    submit(document, "type-rename-form");
    submit(document, "type-rename-form");
    await tick();
    expect(calls).toBe(1);
  });

  it("explains an in-use type as a toast — no confirm, and NO delete is sent", async () => {
    // RESISTOR.deletable is false (2 components). The reason is shown from the row's
    // own counts and NOTHING is sent — a stale advisory flag must never turn into an
    // unprompted irreversible delete.
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.confirm = vi.fn(() => true);

    window.deleteType(RESISTOR);
    await tick();

    expect(window.confirm).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([, o]) => o?.method === "DELETE")).toBe(false);
    expect(document.querySelector(".toast").textContent).toContain("2 components");
  });

  it("confirms before deleting a type that can be deleted", async () => {
    const { window, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.confirm = vi.fn(() => true);

    window.deleteType({ id: 7, name: "throwaway", deletable: true });
    await tick();

    expect(window.confirm).toHaveBeenCalled();
    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/types/7");
    expect(call[1].method).toBe("DELETE");
  });
});

describe("types_admin.js — parameter writes", () => {
  it("edits a parameter, sending the WHOLE body incl. newline-split enum tokens", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamsDialog(RESISTOR);
    document.querySelector("#type-params-list li button").click(); // the Edit button

    const form = document.getElementById("param-edit-form");
    expect(form.definition_id.value).toBe("9");
    expect(form.elements.data_type.value).toBe("enum");
    expect(form.querySelector(".param-enum").hidden).toBe(false);
    // Tokens are pre-filled one per line (a comma is a legal character in a token).
    expect(form.elements.enum_values.value).toBe("C0G\nX7R");

    form.elements.label.value = "Dielectric material";
    form.elements.enum_values.value = "C0G\nX7R\nY5V";
    submit(document, "param-edit-form");
    await tick();

    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/parameters/9");
    expect(call[1].method).toBe("PATCH");
    // The full body — every field, so a dropped field is caught (the API is a
    // partial PATCH, so an omitted field would silently keep its old value).
    expect(JSON.parse(call[1].body)).toEqual({
      name: "dielectric",
      label: "Dielectric material",
      unit: null,
      sort_order: 0,
      is_table_column: true,
      is_filterable: false,
      enum_values: ["C0G", "X7R", "Y5V"],
    });
  });

  it("omits enum_values when editing a non-enum parameter", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamEditDialog({
      id: 4,
      name: "resistance",
      label: "Resistance",
      data_type: "number",
      unit: "Ω",
      sort_order: 1,
      is_table_column: false,
      is_filterable: true,
      enum_values: [],
    });
    expect(document.querySelector("#param-edit-form .param-enum").hidden).toBe(true);

    submit(document, "param-edit-form");
    await tick();

    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/parameters/4");
    expect(JSON.parse(call[1].body).enum_values).toBeUndefined();
  });

  it("shows a parameter's in-use count in its detail line", () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamsDialog({
      ...RESISTOR,
      parameters: [{ ...DIELECTRIC, deletable: false, in_use_count: 3 }],
    });
    expect(document.querySelector(".param-list-detail").textContent).toContain(
      "3 in use",
    );
  });

  it("deletes a parameter via DELETE", async () => {
    const { window, document, fetchMock } = loadPage(typesAdminPageFixture(), SCRIPTS);
    window.openParamsDialog(RESISTOR);
    document.querySelectorAll("#type-params-list li button")[1].click(); // Delete
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

    const call = fetchMock.mock.calls.find(([u]) => u === "/api/types/3/parameters");
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

  it("opens the shared New Type builder and reloads the table on create", async () => {
    const page = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: () => ok({ data: [] }),
    });
    const openTypeDialog = vi.fn();
    page.window.openTypeDialog = openTypeDialog; // provided by type_dialog.js on the page

    page.document.getElementById("new-type-btn").click();
    expect(openTypeDialog).toHaveBeenCalled();

    // Its onCreated callback reloads the types feed so the new type shows up.
    openTypeDialog.mock.calls[0][0]();
    await tick();
    expect(page.fetchMock.mock.calls.some(([u]) => u === "/web/api/types")).toBe(true);
  });

  it("creates a type via the REAL builder and offers it as a parent next time", async () => {
    // The one place type_dialog.js and types_admin.js run together (as on /types):
    // button → builder → POST → the created type becomes a selectable parent, then
    // the table reloads. The parent-select upsert is the core taxonomy-building flow.
    const page = loadPage(
      typesAdminPageFixture(),
      ["shared.js", "type_dialog.js", "types_admin.js"],
      {
        fetchImpl: (url, opts) =>
          url === "/api/types" && opts?.method === "POST"
            ? ok({ id: 8, name: JSON.parse(opts.body).name })
            : ok({ data: [] }),
      },
    );
    const { document, fetchMock } = page;

    document.getElementById("new-type-btn").click();
    document.querySelector('#type-form [name="type-name"]').value = "cable";
    submit(document, "type-form");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([u, o]) => u === "/api/types" && o.method === "POST",
    );
    expect(JSON.parse(post[1].body).name).toBe("cable");
    // The new type is now a valid parent for the next one built this session…
    const parent = document.querySelector('#type-form [name="parent-id"]');
    expect([...parent.options].some((o) => o.value === "8")).toBe(true);
    // …and the /types table reloaded.
    expect(fetchMock.mock.calls.some(([u]) => u === "/web/api/types")).toBe(true);
  });

  it("uses independent guards: an in-flight rename doesn't block a param delete", async () => {
    // types_admin.js calls makeGuard() once per action on purpose; collapsing them to
    // one shared lock would let a hung rename freeze every other write.
    const page = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        url.startsWith("/api/admin/types/")
          ? new Promise(() => {}) // the rename hangs, holding its own guard
          : ok({ data: [] }),
    });
    const { window, document, fetchMock } = page;
    window.confirm = vi.fn(() => true);

    window.openRenameDialog({ id: 3, name: "resistor" });
    document.getElementById("type-rename-form").elements.name.value = "res";
    submit(document, "type-rename-form");
    await tick();

    window.deleteParam({ id: 9, label: "dielectric", deletable: true });
    await tick();

    // The param delete went through despite the stuck rename — separate guards.
    expect(
      fetchMock.mock.calls.some(
        ([u, o]) => u === "/api/admin/parameters/9" && o?.method === "DELETE",
      ),
    ).toBe(true);
  });

  it("re-renders an open parameters list after a reload", async () => {
    const updated = {
      ...RESISTOR,
      parameters: [DIELECTRIC, { ...DIELECTRIC, id: 10, name: "tolerance" }],
    };
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS, {
      fetchImpl: () => ok({ data: [updated] }),
    });
    window.openParamsDialog(RESISTOR); // one parameter shown
    expect(document.querySelectorAll("#type-params-list li").length).toBe(1);

    await window.loadTypes(); // feed now has two — the open list must follow
    await tick();
    expect(document.querySelectorAll("#type-params-list li").length).toBe(2);
  });

  it("opens the matchers panel scoped to a parameter, when it's available", () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS);
    // The matchers panel is only present for admins; stub its opener before render.
    window.openParamMatchers = vi.fn();
    window.openParamsDialog(RESISTOR); // renders DIELECTRIC (id 9), type id 3
    const matcher = [
      ...document.querySelectorAll("#type-params-list li button"),
    ].find((b) => b.textContent === "Matchers");
    expect(matcher).toBeTruthy();
    matcher.click();
    expect(window.openParamMatchers).toHaveBeenCalledWith(RESISTOR.parameters[0], 3);
  });

  it("omits the matchers button when the panel isn't on the page", () => {
    const { window, document } = loadPage(typesAdminPageFixture(), SCRIPTS);
    // No window.openParamMatchers (non-admin) → no button.
    window.openParamsDialog(RESISTOR);
    const labels = [
      ...document.querySelectorAll("#type-params-list li button"),
    ].map((b) => b.textContent);
    expect(labels).not.toContain("Matchers");
  });
});

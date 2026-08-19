import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, typePageFixture, componentPageFixture } from "./harness.js";

// type_dialog.js in isolation — no app.js. This is the reusable module that makes
// "+ New type" work on the invoice page: it exposes window.openTypeDialog and, on a
// successful create, fires the caller's callback with the created type.
const SCRIPTS = ["shared.js", "type_dialog.js"];

function fire(el, type) {
  el.dispatchEvent(
    new el.ownerDocument.defaultView.Event(type, { cancelable: true, bubbles: true }),
  );
}

describe("type_dialog.js (standalone)", () => {
  it("exposes openTypeDialog and opens the dialog", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    expect(typeof window.openTypeDialog).toBe("function");
    window.openTypeDialog(() => {});
    // resetTypeForm ran (empty hint shown, no rows) and the dialog was opened.
    expect(document.getElementById("params-empty").hidden).toBe(false);
  });

  it("adds parameter rows and toggles the empty hint", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    window.openTypeDialog(() => {});
    document.getElementById("add-param").click();
    expect(document.querySelectorAll("#params .param-row").length).toBe(1);
    expect(document.getElementById("params-empty").hidden).toBe(true);
  });

  it("POSTs the type and fires the caller's callback with the created type", async () => {
    let created = null;
    const fetchImpl = (url, opts) =>
      url === "/api/types" && opts?.method === "POST"
        ? Promise.resolve({ ok: true, json: async () => ({ id: 9, name: "cap" }) })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { window, document, fetchMock } = loadPage(typePageFixture(), SCRIPTS, {
      fetchImpl,
    });
    // The invoice-page path: the callback selects the new type in the component dialog.
    window.openTypeDialog((type) => {
      created = type;
    });
    document.querySelector('[name="type-name"]').value = "cap";
    fire(document.getElementById("type-form"), "submit");
    await tick();

    expect(fetchMock.mock.calls.some(([u]) => u === "/api/types")).toBe(true);
    expect(created).toEqual({ id: 9, name: "cap" });
  });

  it("does not fire the callback and shows the error on a failed create", async () => {
    let fired = false;
    const fetchImpl = (url, opts) =>
      url === "/api/types" && opts?.method === "POST"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "name taken" }) })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { window, document } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    window.openTypeDialog(() => {
      fired = true;
    });
    document.querySelector('[name="type-name"]').value = "cap";
    fire(document.getElementById("type-form"), "submit");
    await tick();

    expect(fired).toBe(false);
    expect(document.getElementById("type-error").textContent).toBe("name taken");
  });

  it("shows a reach-the-server message when the create request throws", async () => {
    const fetchImpl = (url) =>
      url === "/api/types"
        ? Promise.reject(new Error("network"))
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { window, document } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    window.openTypeDialog(() => {});
    document.querySelector('[name="type-name"]').value = "cap";
    fire(document.getElementById("type-form"), "submit");
    await tick();
    const error = document.getElementById("type-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/reach the server/i);
  });

  it("does nothing on a page without the type dialog", () => {
    const { window } = loadPage("<div></div>", SCRIPTS);
    expect(window.openTypeDialog).toBeUndefined();
  });
});

// "Create matcher" on a parameter row: a matcher needs a persisted parameter, so the
// button saves the type first, then opens the matcher dialog scoped to the new param.
describe("type_dialog.js — inline Create matcher (save-then-scope)", () => {
  function setup(created) {
    const fetchImpl = (url, opts) =>
      url === "/api/types" && opts?.method === "POST"
        ? Promise.resolve({ ok: true, json: async () => created })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const page = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    // The button only wires up when the matcher dialog is present (admin); stub it.
    page.window.openMatcherDialog = vi.fn(() => Promise.resolve());
    return page;
  }

  it("saves the type, then opens the matcher scoped to the fresh parameter id", async () => {
    let createdCb = null;
    const created = {
      id: 9,
      name: "cap",
      parameters: [{ id: 42, name: "esr" }],
    };
    const { window, document, fetchMock } = setup(created);
    window.openTypeDialog((t) => {
      createdCb = t;
    });
    document.querySelector('[name="type-name"]').value = "cap";
    document.getElementById("add-param").click();
    const row = document.querySelector("#params .param-row");
    row.querySelector('[name="p-name"]').value = "esr";
    row.querySelector('[name="p-label"]').value = "ESR";
    row.querySelector(".param-create-matcher").click();
    await tick();
    await tick();

    // The type was created…
    expect(
      fetchMock.mock.calls.some(([u, o]) => u === "/api/types" && o?.method === "POST"),
    ).toBe(true);
    expect(createdCb).toEqual(created); // the normal onCreated still fired
    // …and the matcher opened pre-scoped to that parameter.
    expect(window.openMatcherDialog).toHaveBeenCalledWith(null, {
      domain: "param_name",
      typeId: 9,
      parameterDefinitionId: 42,
    });
  });

  it("won't save (or open the matcher) for a nameless parameter", async () => {
    const { window, document, fetchMock } = setup({ id: 9, name: "cap", parameters: [] });
    window.openTypeDialog(() => {});
    document.querySelector('[name="type-name"]').value = "cap";
    document.getElementById("add-param").click();
    // Leave p-name empty.
    document.querySelector(".param-create-matcher").click();
    await tick();

    expect(fetchMock.mock.calls.some(([u]) => u === "/api/types")).toBe(false);
    expect(window.openMatcherDialog).not.toHaveBeenCalled();
    expect(document.getElementById("type-error").hidden).toBe(false);
  });

  it("keeps the button hidden when the matcher dialog isn't on the page", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    // No window.openMatcherDialog (non-admin).
    window.openTypeDialog(() => {});
    document.getElementById("add-param").click();
    expect(document.querySelector(".param-create-matcher").hidden).toBe(true);
  });
});

// The exact scenario this feature delivers: the invoice/BOM script set — the shared
// component dialog + the type builder, WITHOUT app.js (which is list-page only).
// "+ New type" from the component dialog must create a type and select it.
describe("type_dialog.js + component_dialog.js (invoice-style, no app.js)", () => {
  const INVOICE_SCRIPTS = ["shared.js", "component_dialog.js", "type_dialog.js"];

  it("creates a type from the component dialog and selects it", async () => {
    const impl = (url, opts) => {
      if (url === "/api/types" && opts?.method === "POST") {
        return Promise.resolve({ ok: true, json: async () => ({ id: 7, name: "capacitor" }) });
      }
      if (url.startsWith("/api/types/") && url.endsWith("/parameters")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { window, document } = loadPage(componentPageFixture(), INVOICE_SCRIPTS, {
      fetchImpl: impl,
    });
    // No list page here, so open the shared dialog directly (as the invoice line does).
    window.openComponentDialog(() => {});
    const newTypeBtn = document.getElementById("component-new-type");
    expect(newTypeBtn.hidden).toBe(false); // the type builder is present on this page
    newTypeBtn.click(); // opens the type dialog via window.openTypeDialog

    document.getElementById("type-form").querySelector('[name="type-name"]').value =
      "capacitor";
    fire(document.getElementById("type-form"), "submit");
    await tick();

    const select = document.getElementById("component-type");
    const option = [...select.options].find((o) => o.value === "7");
    expect(option).toBeTruthy();
    expect(option.textContent).toBe("capacitor");
    expect(select.value).toBe("7"); // the new type is selected in the component dialog
  });
});

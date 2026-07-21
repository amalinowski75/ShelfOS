import { describe, it, expect } from "vitest";
import { loadPage, tick, typePageFixture } from "./harness.js";

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

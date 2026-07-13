import { describe, it, expect, vi } from "vitest";
import {
  loadPage,
  tick,
  CSRF,
  newInvoiceFixture,
  detailFixture,
  componentDialogFixture,
  fetchBody,
} from "./harness.js";

// location_tree.js enhances the line dialog's location picker (§7).
const SCRIPTS = ["shared.js", "location_tree.js", "invoices.js"];

function submit(document, formId) {
  document
    .getElementById(formId)
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("invoices.js — new invoice", () => {
  it("posts the metadata and sends empty notes as null", async () => {
    const { document, fetchMock } = loadPage(newInvoiceFixture(), SCRIPTS);
    const form = document.getElementById("invoice-new-form");
    form.supplier.value = "Mouser";
    form.invoice_number.value = "INV-1";
    form.invoice_date.value = "2026-07-08";
    form.currency.value = "EUR";
    form.notes.value = "";

    submit(document, "invoice-new-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({
      supplier: "Mouser",
      invoice_number: "INV-1",
      invoice_date: "2026-07-08",
      currency: "EUR",
      notes: null,
    });
  });
});

describe("invoices.js — edit metadata", () => {
  it("sends null for untouched notes but an explicit '' when cleared", async () => {
    const untouched = loadPage(detailFixture({ notes: "rush" }), SCRIPTS);
    untouched.document.getElementById("invoice-edit-btn").click();
    submit(untouched.document, "invoice-meta-form");
    await tick();
    expect(untouched.fetchMock.mock.calls[0][0]).toBe("/api/invoices/7");
    expect(untouched.fetchMock.mock.calls[0][1].method).toBe("PATCH");
    expect(untouched.fetchMock.mock.calls[0][1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(untouched.fetchMock).notes).toBeNull();

    const cleared = loadPage(detailFixture({ notes: "rush" }), SCRIPTS);
    cleared.document.getElementById("invoice-edit-btn").click();
    cleared.document.getElementById("invoice-meta-form").notes.value = "";
    submit(cleared.document, "invoice-meta-form");
    await tick();
    expect(fetchBody(cleared.fetchMock).notes).toBe("");
  });
});

describe("invoices.js — edit line", () => {
  it("sends null for untouched SPN but an explicit '' when cleared", async () => {
    const untouched = loadPage(detailFixture({ lineSpn: "SPN-9" }), SCRIPTS);
    untouched.document.querySelector('[data-act="edit-line"]').click();
    submit(untouched.document, "invoice-line-form");
    await tick();
    expect(untouched.fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/lines/3");
    expect(untouched.fetchMock.mock.calls[0][1].method).toBe("PUT");
    expect(untouched.fetchMock.mock.calls[0][1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(untouched.fetchMock).supplier_part_number).toBeNull();

    const cleared = loadPage(detailFixture({ lineSpn: "SPN-9" }), SCRIPTS);
    cleared.document.querySelector('[data-act="edit-line"]').click();
    cleared.document.getElementById("invoice-line-form").supplier_part_number.value =
      "";
    submit(cleared.document, "invoice-line-form");
    await tick();
    expect(fetchBody(cleared.fetchMock).supplier_part_number).toBe("");
  });

  it("rejects clearing a set location instead of silently ignoring it", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
    );
    document.querySelector('[data-act="edit-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "";
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    const error = document.getElementById("invoice-line-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/can't be cleared/);
  });

  it("rejects clearing a location picked via the — none — node", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
    );
    document.querySelector('[data-act="edit-line"]').click(); // prefilled to D1 (5)
    // Clear through the widget, not by writing the hidden input directly.
    document.querySelector(".loc-picker-none").click();
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    const error = document.getElementById("invoice-line-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/can't be cleared/);
  });

  it("applies a changed location through the location endpoint", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
    );
    document.querySelector('[data-act="edit-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "9";
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/lines/3");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/invoices/7/lines/3/location");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ location_id: 9 });
  });
});

describe("invoices.js — remove line and finalize", () => {
  it("deletes a line after confirmation, with the CSRF header", async () => {
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    document.querySelector('[data-act="remove-line"]').click();
    await tick();

    expect(window.confirm).toHaveBeenCalled();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/lines/3");
    expect(opts.method).toBe("DELETE");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("does not delete when the confirm is dismissed", async () => {
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    window.confirm.mockReturnValue(false);
    document.querySelector('[data-act="remove-line"]').click();
    await tick();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("finalize sends a null gross when the field is blank", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ withFinalize: true }),
      SCRIPTS,
    );
    document.getElementById("invoice-finalize-btn").click();
    submit(document, "invoice-finalize-form");
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/finalize");
    expect(fetchBody(fetchMock).total_gross).toBeNull();
  });
});

describe("invoices.js — error surfacing and add-line", () => {
  it("shows the server error message when a write fails", async () => {
    const { document, fetchMock } = loadPage(newInvoiceFixture(), SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          json: async () => ({ detail: "invoice 'INV-1' already exists" }),
        }),
    });
    const form = document.getElementById("invoice-new-form");
    form.supplier.value = "Mouser";
    form.invoice_number.value = "INV-1";
    form.invoice_date.value = "2026-07-08";
    form.currency.value = "EUR";
    form.notes.value = "";

    submit(document, "invoice-new-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const error = document.getElementById("invoice-new-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("invoice 'INV-1' already exists");
  });

  it("reports a partial failure when only the location step fails", async () => {
    // The line PUT succeeds; the follow-up location PUT fails.
    const fetchImpl = (url) =>
      Promise.resolve({
        ok: !url.endsWith("/location"),
        json: async () => ({ detail: "location was removed" }),
      });
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
      { fetchImpl },
    );
    document.querySelector('[data-act="edit-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "9";
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const error = document.getElementById("invoice-line-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/location could not be set/);
    expect(error.textContent).toMatch(/location was removed/);
  });

  it("populates the component picker and posts a new line", async () => {
    const fetchImpl = (url) =>
      url === "/web/api/components"
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              data: [
                { id: 11, mpn: "R-1", manufacturer: "Yageo", type: "resistor" },
              ],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({ id: 99 }) });

    const { document, fetchMock } = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("invoice-addline-btn").click();
    await tick(); // let loadComponentOptions resolve and fill the <select>

    const select = document.getElementById("invoice-line-form").component_id;
    expect([...select.options].map((o) => o.value)).toEqual(["11"]);
    expect(select.value).toBe("11");

    const form = document.getElementById("invoice-line-form");
    form.quantity.value = "3";
    form.unit_price.value = "1.50";
    form.supplier_part_number.value = "";
    form.location_id.value = "";
    submit(document, "invoice-line-form");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/invoices/7/lines" && opts.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse(post[1].body)).toEqual({
      component_id: 11,
      quantity: 3,
      unit_price: "1.50",
      supplier_part_number: null,
      location_id: null,
    });
  });

  it("picks a location for the new line through the tree-picker", async () => {
    const fetchImpl = (url) =>
      url === "/web/api/components"
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              data: [{ id: 11, mpn: "R-1", manufacturer: null, type: "resistor" }],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({ id: 99 }) });
    const { document, fetchMock } = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("invoice-addline-btn").click();
    await tick();

    const form = document.getElementById("invoice-line-form");
    form.quantity.value = "2";
    form.unit_price.value = "1";
    // Select a location through the widget (node D1 = id 5).
    document.querySelector('.loc-picker-node[data-loc-id="5"]').click();
    expect(form.location_id.value).toBe("5");
    submit(document, "invoice-line-form");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/invoices/7/lines" && opts.method === "POST",
    );
    expect(JSON.parse(post[1].body).location_id).toBe(5);
  });

  it("edit line reflects the existing location in the picker", () => {
    const { document } = loadPage(detailFixture({ lineLocationId: "5" }), SCRIPTS);
    document.querySelector('[data-act="edit-line"]').click();
    // openEditLine calls the picker's setValue -> hidden input and label follow.
    expect(document.querySelector('[name="location_id"]').value).toBe("5");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "D1",
    );
  });

  it("does not leak a location between lines when switching edits", () => {
    const { document } = loadPage(
      detailFixture({ lineLocationId: "5", secondLine: true }),
      SCRIPTS,
    );
    const editButtons = document.querySelectorAll('[data-act="edit-line"]');
    editButtons[0].click(); // line 3 has D1
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "D1",
    );
    editButtons[1].click(); // line 4 has no location -> reset() clears the prior pick
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "— none —",
    );
  });

  it("restores the placeholder when the add-line dialog is reopened", async () => {
    const fetchImpl = (url) =>
      url === "/web/api/components"
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              data: [{ id: 11, mpn: "R", manufacturer: null, type: "resistor" }],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { document } = loadPage(detailFixture(), SCRIPTS, { fetchImpl });

    document.getElementById("invoice-addline-btn").click();
    await tick();
    document.querySelector('.loc-picker-node[data-loc-id="5"]').click(); // pick D1
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "D1",
    );

    document.getElementById("invoice-addline-btn").click(); // reopen -> reset
    await tick();
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "— none —",
    );
  });

  it("guards against a double submit while a write is in flight", async () => {
    const { document, fetchMock } = loadPage(newInvoiceFixture(), SCRIPTS, {
      fetchImpl: () =>
        new Promise((resolve) =>
          setTimeout(
            () => resolve({ ok: true, json: async () => ({ id: 1 }) }),
            15,
          ),
        ),
    });
    const form = document.getElementById("invoice-new-form");
    form.supplier.value = "Mouser";
    form.invoice_number.value = "INV-1";
    form.invoice_date.value = "2026-07-08";
    form.currency.value = "EUR";
    form.notes.value = "";

    submit(document, "invoice-new-form");
    submit(document, "invoice-new-form"); // second click while the first is in flight
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("invoices.js — new component from the add-line flow", () => {
  const SHARED = [
    "shared.js",
    "component_dialog.js",
    "location_tree.js",
    "invoices.js",
  ];

  it("creates a component in the picker and selects it, without new backend", async () => {
    // The picker feed deliberately never includes the new component, proving the
    // new option is inserted directly from the create response, not via a reload.
    const fetchImpl = (url, opts) => {
      if (url === "/web/api/components") {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            columns: [],
            data: [{ id: 5, mpn: "OLD", manufacturer: null, type: "resistor" }],
          }),
        });
      }
      if (url.startsWith("/api/types/") && url.endsWith("/parameters")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      if (url === "/api/components" && opts?.method === "POST") {
        return Promise.resolve({
          ok: true,
          json: async () => ({ id: 99, type_id: 1, mpn: "NEW", manufacturer: "Acme" }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };

    const { document } = loadPage(
      detailFixture() + componentDialogFixture(),
      SHARED,
      { fetchImpl },
    );

    // Per-instance close spies so we can prove only the inner dialog closes.
    const lineClose = vi.fn();
    const componentClose = vi.fn();
    document.getElementById("invoice-line-dialog").close = lineClose;
    document.getElementById("component-dialog").close = componentClose;

    document.getElementById("invoice-addline-btn").click();
    await tick(); // picker populated with the existing component (#5)

    document.getElementById("invoice-add-component-btn").click(); // open shared dialog
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "1";
    typeSelect.dispatchEvent(
      new document.defaultView.Event("change", { bubbles: true }),
    );
    await tick();
    submit(document, "component-form"); // create the component
    await tick();

    // The new component is now an option in the line picker, and selected,
    // even though the reload feed never contained it.
    const picker = document.querySelector('[name="component_id"]');
    expect([...picker.options].map((o) => o.value)).toContain("99");
    expect(picker.value).toBe("99");
    const newOption = picker.querySelector('option[value="99"]');
    expect(newOption.textContent).toBe("NEW · Acme");
    // Only the stacked component dialog closes; the line dialog stays open.
    expect(componentClose).toHaveBeenCalled();
    expect(lineClose).not.toHaveBeenCalled();
  });
});

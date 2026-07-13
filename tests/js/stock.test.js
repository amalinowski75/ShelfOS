import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

// app.js drives the stock dialog; location_tree.js enhances its picker.
const SCRIPTS = ["shared.js", "location_tree.js", "app.js"];

// The components page essentials app.js needs at load, plus a stock dialog whose
// location field is the tree-picker (one selectable node).
function stockPageFixture() {
  return `
    <select id="type-filter"></select>
    <div id="components-table"></div>
    <dialog id="stock-dialog">
      <strong id="stock-dialog-title"></strong>
      <form id="stock-form">
        <input name="component_id" />
        <input name="mode" />
        <input name="quantity" type="number" />
        <div class="loc-picker">
          <input type="hidden" name="location_id" value="" />
          <button type="button" class="loc-picker-toggle">
            <span class="loc-picker-label">Select a location…</span>
          </button>
          <div class="loc-picker-menu" hidden>
            <ul class="loc-picker-list"><li>
              <div class="loc-picker-row">
                <span class="loc-picker-caret-spacer"></span>
                <button type="button" class="loc-picker-node"
                        data-loc-id="5" data-loc-path="Lab / D1">D1</button>
              </div>
            </li></ul>
          </div>
        </div>
        <input name="note" />
        <p id="stock-error" hidden></p>
        <button type="submit"></button>
      </form>
    </dialog>`;
}

function submitStock(document) {
  document
    .getElementById("stock-form")
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("app.js — stock dialog with the location tree-picker", () => {
  it("requires a location: submitting with none picked shows an error, no POST", async () => {
    const { window, document, fetchMock } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("add", { id: 7 }); // resets the picker to empty
    submitStock(document);
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    const error = document.getElementById("stock-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/Choose a location/);
  });

  it("posts the picked location once the tree-picker has a selection", async () => {
    const fetchImpl = (url) =>
      Promise.resolve({
        ok: true,
        json: async () =>
          url.startsWith("/web/api/components") ? { columns: [], data: [] } : {},
      });
    const { window, document, fetchMock } = loadPage(stockPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    window.openStockDialog("add", { id: 7 });
    // Pick a location through the widget.
    document.querySelector(".loc-picker-node").click();
    document.querySelector("#stock-form").quantity.value = "3";
    submitStock(document);
    await tick();

    const post = fetchMock.mock.calls.find(([url]) => url === "/api/stock/add");
    expect(post).toBeTruthy();
    expect(post[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(post[1].body)).toEqual({
      component_id: 7,
      location_id: 5,
      quantity: 3,
      note: null,
    });
  });

  it("releases the guard after a validation error so a corrected resubmit posts", async () => {
    const fetchImpl = (url) =>
      url === "/api/stock/add"
        ? Promise.resolve({ ok: true, json: async () => ({}) })
        : Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    const { window, document, fetchMock } = loadPage(stockPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    window.openStockDialog("add", { id: 7 });
    submitStock(document); // no location -> validation error, no POST, guard released
    await tick();
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length).toBe(0);

    document.querySelector(".loc-picker-node").click(); // fix the location
    submitStock(document);
    await tick();
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length).toBe(1);
  });

  it("ignores a second submit while the stock POST is in flight", async () => {
    let resolveFetch;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    const fetchImpl = (url) =>
      url === "/api/stock/add"
        ? pending
        : Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    const { window, document, fetchMock } = loadPage(stockPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    window.openStockDialog("add", { id: 7 });
    document.querySelector(".loc-picker-node").click(); // satisfy the location check
    submitStock(document); // POST now in flight
    submitStock(document); // a fast double-click must be ignored
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length).toBe(1);
    resolveFetch({ ok: true, json: async () => ({}) });
    await tick();
  });

  it("reset clears a prior selection between opens", () => {
    const { window, document } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("add", { id: 7 });
    document.querySelector(".loc-picker-node").click(); // location_id = 5
    expect(document.querySelector('[name="location_id"]').value).toBe("5");
    window.openStockDialog("take", { id: 8 }); // reopen -> reset
    expect(document.querySelector('[name="location_id"]').value).toBe("");
  });
});

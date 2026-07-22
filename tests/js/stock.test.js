import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

// stock_dialog.js owns the dialog (shared by the components list and a component's
// detail page); location_tree.js enhances its picker. No app.js — the dialog stands
// on its own, which is the whole point of the split.
const SCRIPTS = ["shared.js", "location_tree.js", "stock_dialog.js"];

// A stock dialog whose location field is the tree-picker (one selectable node).
function stockPageFixture() {
  return `
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

describe("stock_dialog.js — the shared Add/Take dialog", () => {
  it("requires a location: submitting with none picked shows an error, no POST", async () => {
    const { window, document, fetchMock } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("add", 7); // resets the picker to empty
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
    window.openStockDialog("add", 7);
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
    window.openStockDialog("add", 7);
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
    window.openStockDialog("add", 7);
    document.querySelector(".loc-picker-node").click(); // satisfy the location check
    submitStock(document); // POST now in flight
    submitStock(document); // a fast double-click must be ignored
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length).toBe(1);
    resolveFetch({ ok: true, json: async () => ({}) });
    await tick();
  });

  it("reset clears a prior selection between opens", () => {
    const { window, document } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("add", 7);
    document.querySelector(".loc-picker-node").click(); // location_id = 5
    expect(document.querySelector('[name="location_id"]').value).toBe("5");
    window.openStockDialog("take", 8); // reopen -> reset
    expect(document.querySelector('[name="location_id"]').value).toBe("");
  });

  it("reports a rejected fetch instead of sitting there unchanged", async () => {
    // Offline/DNS/aborted: silence reads as "nothing happened" and invites a second
    // click on a write that may or may not have landed.
    const { window, document } = loadPage(stockPageFixture(), SCRIPTS, {
      fetchImpl: () => Promise.reject(new Error("offline")),
    });
    window.openStockDialog("add", 7);
    document.querySelector(".loc-picker-node").click();
    submitStock(document);
    await tick();
    const error = document.getElementById("stock-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/Could not reach the server/);
  });

  it("clears the note between opens so it can't leak into the next movement", () => {
    const { window, document } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("add", 7);
    document.querySelector("#stock-form").note.value = "damaged reel";
    window.openStockDialog("add", 8);
    expect(document.querySelector("#stock-form").note.value).toBe("");
  });

  it("posts to /api/stock/remove in take mode", async () => {
    const { window, document, fetchMock } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("take", 7);
    document.querySelector(".loc-picker-node").click();
    submitStock(document);
    await tick();
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/stock/remove")).toBe(true);
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/stock/add")).toBe(false);
  });

  it("runs onSaved only after a successful write, and not on failure", async () => {
    // The callback is how each page refreshes — the list re-pulls its feed, a
    // server-rendered page reloads. Running it on a rejected write would hide the
    // error message under a refresh.
    let ok = false;
    const fetchImpl = () => Promise.resolve({ ok, json: async () => ({ detail: "no" }) });
    const { window, document } = loadPage(stockPageFixture(), SCRIPTS, { fetchImpl });
    const saved = [];

    window.openStockDialog("add", 7, () => saved.push("called"));
    document.querySelector(".loc-picker-node").click();
    submitStock(document);
    await tick();
    expect(saved).toEqual([]);
    const error = document.getElementById("stock-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("no");

    ok = true;
    submitStock(document);
    await tick();
    expect(saved).toEqual(["called"]);
  });
});

describe("stock_dialog.js — [data-stock-act] triggers (component detail page)", () => {
  // The detail page is server-rendered and has no JSON feed, so it declares its
  // buttons in markup rather than wiring them itself.
  const detailFixture = () =>
    `<button data-stock-act="add" data-component-id="42">Add</button>
     <button data-stock-act="take" data-component-id="42">Take</button>
     ${stockPageFixture()}`;

  it("opens the dialog in add mode for the button's component", () => {
    const { window, document } = loadPage(detailFixture(), SCRIPTS);
    document.querySelector('[data-stock-act="add"]').click();
    expect(document.getElementById("stock-dialog-title").textContent).toBe("Add stock");
    expect(document.getElementById("stock-form").component_id.value).toBe("42");
    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
  });

  it("opens the dialog in take mode from the Take button", () => {
    const { document } = loadPage(detailFixture(), SCRIPTS);
    document.querySelector('[data-stock-act="take"]').click();
    expect(document.getElementById("stock-dialog-title").textContent).toBe(
      "Take from stock",
    );
    expect(document.getElementById("stock-form").mode.value).toBe("take");
  });

  it("posts the component from the button, not a stale one", async () => {
    const { document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    document.querySelector('[data-stock-act="take"]').click();
    document.querySelector(".loc-picker-node").click();
    document.querySelector("#stock-form").quantity.value = "2";
    submitStock(document);
    await tick();
    const post = fetchMock.mock.calls.find(([u]) => u === "/api/stock/remove");
    expect(JSON.parse(post[1].body)).toEqual({
      component_id: 42,
      location_id: 5,
      quantity: 2,
      note: null,
    });
  });

  it("blocks a second write while the post-save reload is still pending", async () => {
    // A reload is a PENDING navigation: the page stays live and clickable until the
    // server answers, so releasing the in-flight guard here would let a second click
    // post a duplicate movement. (jsdom can't navigate; the harness swallows that.)
    const { document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    document.querySelector('[data-stock-act="add"]').click();
    document.querySelector(".loc-picker-node").click();
    submitStock(document);
    await tick();
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length).toBe(1);

    // Everything the user could do next must now be inert. Re-pick the location
    // first: otherwise the reopen's own picker reset would block the second POST
    // and the test would pass whether or not the navigation guard exists.
    document.querySelector('[data-stock-act="add"]').click();
    document.querySelector(".loc-picker-node").click();
    expect(document.querySelector('[name="location_id"]').value).toBe("5");
    submitStock(document);
    await tick();
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length).toBe(1);
  });

  it("renders the detail-page buttons visibly under the real app.css", () => {
    // .row-actions is opacity-0 until its Tabulator row is hovered, so reusing it
    // outside the table would ship two invisible (but clickable) buttons. The
    // attribute assertions in test_web.py can't see that; computed style can.
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(
      `<style>${css}</style>
       <div class="card card-pad"><div class="widget-head"><h2>Stock movements</h2>
         <div class="widget-actions" id="actions"><button class="btn btn-secondary btn-sm"
              id="add" data-stock-act="add" data-component-id="1">Add</button></div>
       </div></div>`,
    );
    const style = (id) =>
      dom.window.getComputedStyle(dom.window.document.getElementById(id));
    // The WRAPPER, not the button: opacity is not inherited, so a transparent
    // parent leaves the child's computed opacity at 1 while hiding it on screen.
    expect(style("actions").opacity).not.toBe("0");
    expect(style("add").display).not.toBe("none");
  });

  it("leaves a button with no component id inert rather than opening on NaN", () => {
    // Number(undefined) is NaN, which JSON.stringify writes as component_id: null —
    // an opaque 422. Refusing to open makes the markup bug obvious instead.
    const { window, document } = loadPage(
      `<button data-stock-act="add">Add</button>${stockPageFixture()}`,
      SCRIPTS,
    );
    document.querySelector("[data-stock-act]").click();
    expect(window.HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
    expect(document.getElementById("stock-form").component_id.value).toBe("");
  });
});

import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

// stock_dialog.js owns the dialog (shared by the components list and a component's
// detail page); location_tree.js enhances its picker. No app.js — the dialog stands
// on its own, which is the whole point of the split.
const SCRIPTS = ["shared.js", "location_tree.js", "stock_dialog.js"];

// One selectable leaf (id 5), enough for the write tests.
function flatTree() {
  return `<ul class="loc-picker-list"><li>
            <div class="loc-picker-row">
              <span class="loc-picker-caret-spacer"></span>
              <button type="button" class="loc-picker-node"
                      data-loc-id="5" data-loc-path="Lab / D1">D1</button>
            </div>
          </li></ul>`;
}

// A nested forest for the filter tests: Lab (1) > Rack A (2) > D1 (5), D2 (6),
// plus a sibling room Store (3) with D9 (9). Mirrors what _location_picker.html
// renders, carets and all.
function nestedTree() {
  const leaf = (id, name, path) =>
    `<li><div class="loc-picker-row"><span class="loc-picker-caret-spacer"></span>
       <button type="button" class="loc-picker-node"
               data-loc-id="${id}" data-loc-path="${path}">${name}</button>
     </div></li>`;
  const branch = (id, name, path, children) =>
    `<li><div class="loc-picker-row">
       <button type="button" class="loc-picker-caret" aria-expanded="false"></button>
       <button type="button" class="loc-picker-node"
               data-loc-id="${id}" data-loc-path="${path}">${name}</button>
     </div>
     <div class="loc-picker-children" hidden>
       <ul class="loc-picker-list">${children}</ul>
     </div></li>`;
  return `<ul class="loc-picker-list">
    ${branch(
      1,
      "Lab",
      "Lab",
      branch(
        2,
        "Rack A",
        "Lab / Rack A",
        leaf(5, "D1", "Lab / Rack A / D1") + leaf(6, "D2", "Lab / Rack A / D2"),
      ),
    )}
    ${branch(3, "Store", "Store", leaf(9, "D9", "Store / D9"))}
  </ul>`;
}

// A stock dialog whose location field is the tree-picker.
function stockPageFixture(tree = flatTree(), creatable = false) {
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
            ${creatable ? '<button type="button" class="loc-picker-new" hidden>+ New location</button>' : ""}
            <label class="loc-picker-showall" hidden>
              <input type="checkbox" class="loc-picker-showall-box" /> show all
            </label>
            <p class="loc-picker-nomatch" hidden>No matching locations</p>
            ${tree}
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

    // The location-usage lookup fires on open; no WRITE may.
    expect(fetchMock.mock.calls.some(([u]) => u.startsWith("/api/stock/"))).toBe(false);
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

  it("toasts when the write landed but the refresh failed", async () => {
    // The dialog is already closed by then, so an inline error would be written
    // into something nobody can see — and "could not reach the server" would be a
    // lie: the movement IS saved, it's the table that's stale.
    const { window, document } = loadPage(stockPageFixture(), SCRIPTS);
    window.openStockDialog("add", 7, () => Promise.reject(new Error("feed down")));
    document.querySelector(".loc-picker-node").click();
    submitStock(document);
    await tick();
    const toast = document.querySelector(".toast");
    expect(toast).toBeTruthy();
    expect(toast.textContent).toMatch(/Stock saved/);
    // Not mislabelled as a network failure on a closed dialog.
    expect(document.getElementById("stock-error").hidden).toBe(true);
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

  it("keeps working after a reload that never lands", async () => {
    // Stop/Esc, a dropped connection or a bfcache restore all leave this page
    // rendered and interactive. A one-way navigation latch would silently kill
    // every stock action from then on, with nothing to say why.
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    const addStock = () => {
      document.querySelector('[data-stock-act="add"]').click();
      document.querySelector(".loc-picker-node").click();
      submitStock(document);
    };
    const posts = () =>
      fetchMock.mock.calls.filter(([u]) => u === "/api/stock/add").length;

    addStock(); // this one requests a reload
    await tick();
    expect(posts()).toBe(1);

    // The page is still here, and pageshow is how it says so.
    window.dispatchEvent(new window.Event("pageshow"));
    addStock();
    await tick();
    expect(posts()).toBe(2);
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

describe("stock_dialog.js — the location filter", () => {
  // Take offers only where the part IS; Add offers a free slot, or the one it
  // already occupies so a restock can go back in.
  const usage = (holding, occupied) => (url) =>
    url.endsWith("/location-usage")
      ? Promise.resolve({ ok: true, json: async () => ({ holding, occupied }) })
      : Promise.resolve({ ok: true, json: async () => ({}) });

  // The dialog fetches usage on open; two ticks covers fetch + json.
  async function open(mode, fetchImpl) {
    const handles = loadPage(stockPageFixture(nestedTree()), SCRIPTS, { fetchImpl });
    handles.window.openStockDialog(mode, 7);
    await tick();
    await tick();
    handles.node = (id) =>
      handles.document.querySelector(`.loc-picker-node[data-loc-id="${id}"]`);
    // "Offered" = pickable: visible in the tree AND not disabled.
    handles.offered = (id) => {
      const node = handles.node(id);
      return !node.disabled && !node.closest("li").hidden;
    };
    return handles;
  }

  it("take offers only the locations holding this component", async () => {
    const h = await open("take", usage([6], [5, 6, 9]));
    expect(h.offered(6)).toBe(true);
    expect(h.offered(5)).toBe(false); // holds something, but not this part
    expect(h.offered(9)).toBe(false);
  });

  it("take keeps the ancestors of a match visible, but unselectable", async () => {
    // Otherwise a match nested three levels down would be unreachable — the tree
    // is expandable, you have to walk through Lab and Rack A to see D2.
    const h = await open("take", usage([6], [6]));
    for (const ancestor of [1, 2]) {
      expect(h.node(ancestor).closest("li").hidden).toBe(false);
      expect(h.node(ancestor).disabled).toBe(true);
    }
    // …and EVERY branch on the way down is open, so nothing to hunt for. Checking
    // only the innermost would pass while Lab stayed collapsed and D2 unreachable.
    for (
      let branch = h.node(6).closest(".loc-picker-children");
      branch;
      branch = branch.parentElement.closest(".loc-picker-children")
    ) {
      expect(branch.hidden).toBe(false);
    }
    // A whole subtree with no match is gone, not just greyed out.
    expect(h.node(3).closest("li").hidden).toBe(true);
  });

  it("add offers free slots and the one already holding this component", async () => {
    // 5 holds this part (restock target), 9 holds something else, 6 is free.
    const h = await open("add", usage([5], [5, 9]));
    expect(h.offered(5)).toBe(true);
    expect(h.offered(6)).toBe(true);
    expect(h.offered(9)).toBe(false);
  });

  it("says so when nothing matches, rather than showing an empty dropdown", async () => {
    const h = await open("take", usage([], [5]));
    expect(h.document.querySelector(".loc-picker-nomatch").hidden).toBe(false);
  });

  it("'show all locations' restores the full tree", async () => {
    const h = await open("take", usage([6], [5, 6, 9]));
    const showAll = h.document.querySelector(".loc-picker-showall");
    expect(showAll.hidden).toBe(false); // only offered while a filter is on
    expect(h.offered(9)).toBe(false);

    const box = h.document.querySelector(".loc-picker-showall-box");
    box.checked = true;
    box.dispatchEvent(new h.window.Event("change", { bubbles: true }));
    expect(h.offered(9)).toBe(true);
    expect(h.offered(1)).toBe(true); // ancestors selectable again too
  });

  it("drops a selection the filter then takes away", async () => {
    // The dialog opens on the full tree and narrows a moment later. A location
    // picked in that window must not stay in the hidden input the form POSTs —
    // the dialog would be hiding the very location it was about to submit.
    let landUsage;
    const fetchImpl = (url) =>
      url.endsWith("/location-usage")
        ? new Promise((resolve) => (landUsage = resolve))
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { window, document } = loadPage(stockPageFixture(nestedTree()), SCRIPTS, {
      fetchImpl,
    });
    window.openStockDialog("take", 7);
    document.querySelector('.loc-picker-node[data-loc-id="9"]').click();
    expect(document.querySelector('[name="location_id"]').value).toBe("9");

    landUsage({ ok: true, json: async () => ({ holding: [6], occupied: [6, 9] }) });
    await tick();
    await tick();
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    expect(document.querySelector(".loc-picker-label").textContent).toMatch(/Select/);
  });

  it("drops a selection made under 'show all' when it is unticked again", async () => {
    const h = await open("take", usage([6], [6, 9]));
    const box = h.document.querySelector(".loc-picker-showall-box");
    const toggleShowAll = (checked) => {
      box.checked = checked;
      box.dispatchEvent(new h.window.Event("change", { bubbles: true }));
    };

    toggleShowAll(true);
    h.node(9).click();
    expect(h.document.querySelector('[name="location_id"]').value).toBe("9");
    toggleShowAll(false);
    expect(h.document.querySelector('[name="location_id"]').value).toBe("");
  });

  it("keeps a selection that the filter still accepts", async () => {
    // The clearing above must not be indiscriminate.
    const h = await open("take", usage([6], [6, 9]));
    h.node(6).click();
    const box = h.document.querySelector(".loc-picker-showall-box");
    box.checked = true;
    box.dispatchEvent(new h.window.Event("change", { bubbles: true }));
    box.checked = false;
    box.dispatchEvent(new h.window.Event("change", { bubbles: true }));
    expect(h.document.querySelector('[name="location_id"]').value).toBe("6");
  });

  it("does not let a kept-for-the-path ancestor be selected", async () => {
    const h = await open("take", usage([6], [6]));
    h.node(2).click(); // "Rack A" — visible only as the way down to D2
    expect(h.document.querySelector('[name="location_id"]').value).toBe("");
  });

  it("blocks the picker while the lookup is in flight", async () => {
    // Otherwise the full tree is live and a pick made now gets silently dropped.
    let landUsage;
    const fetchImpl = (url) =>
      url.endsWith("/location-usage")
        ? new Promise((resolve) => (landUsage = resolve))
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { window, document } = loadPage(stockPageFixture(nestedTree()), SCRIPTS, {
      fetchImpl,
    });
    window.openStockDialog("take", 7);
    const toggle = document.querySelector(".loc-picker-toggle");
    expect(toggle.disabled).toBe(true);

    landUsage({ ok: true, json: async () => ({ holding: [6], occupied: [6] }) });
    await tick();
    await tick();
    expect(toggle.disabled).toBe(false);
  });

  it("re-enables the picker even when the lookup fails", async () => {
    const h = await open("take", () => Promise.reject(new Error("offline")));
    expect(h.document.querySelector(".loc-picker-toggle").disabled).toBe(false);
  });

  it("gives up on a lookup that never answers", async () => {
    // A hang is worse than a failure: fetch has no timeout, so without the race
    // the picker stays disabled forever and NO stock can be moved until reload.
    const { window, document } = loadPage(stockPageFixture(nestedTree()), SCRIPTS, {
      fetchImpl: () => new Promise(() => {}), // never settles, never rejects
    });
    // Capture the give-up timer instead of waiting five real seconds for it.
    const pending = [];
    window.setTimeout = (fn, ms) => pending.push({ fn, ms });

    window.openStockDialog("take", 7);
    const toggle = document.querySelector(".loc-picker-toggle");
    expect(toggle.disabled).toBe(true);

    pending.filter((t) => t.ms === 5000).forEach((t) => t.fn());
    await tick();
    await tick();
    expect(toggle.disabled).toBe(false);
    // …and the full tree is usable, so the write is still possible.
    const d9 = document.querySelector('.loc-picker-node[data-loc-id="9"]');
    expect(d9.disabled).toBe(false);
    expect(d9.closest("li").hidden).toBe(false);
  });

  it("explains a dropped selection rather than silently blanking the toggle", async () => {
    const h = await open("take", usage([6], [6, 9]));
    const box = h.document.querySelector(".loc-picker-showall-box");
    const toggleShowAll = (checked) => {
      box.checked = checked;
      box.dispatchEvent(new h.window.Event("change", { bubbles: true }));
    };
    toggleShowAll(true);
    h.node(9).click();
    toggleShowAll(false);

    const notice = h.document.querySelector(".loc-picker-nomatch");
    expect(notice.hidden).toBe(false);
    expect(notice.textContent).toMatch(/isn't offered here/);
  });

  it("says nothing extra when there are no locations at all", async () => {
    // "No matching locations" + "show all" would both be answering a question the
    // macro's own "No locations yet" line has already answered.
    const empty = `<p class="loc-picker-empty">No locations yet — add one.</p>`;
    const { window, document } = loadPage(stockPageFixture(empty), SCRIPTS, {
      fetchImpl: usage([], []),
    });
    window.openStockDialog("take", 7);
    await tick();
    await tick();
    expect(document.querySelector(".loc-picker-empty").hidden).toBe(false);
    expect(document.querySelector(".loc-picker-nomatch").hidden).toBe(true);
    expect(document.querySelector(".loc-picker-showall").hidden).toBe(true);
  });

  it("does not submit the form on Enter in 'show all'", async () => {
    // A checkbox implicitly submits its form on Enter; here that would post a
    // stock movement from a keyboard user just trying to widen the list.
    const h = await open("take", usage([6], [6, 9]));
    h.node(6).click(); // a valid location, so nothing else would block a submit
    const box = h.document.querySelector(".loc-picker-showall-box");
    const event = new h.window.KeyboardEvent("keydown", {
      key: "Enter",
      cancelable: true,
      bubbles: true,
    });
    box.dispatchEvent(event);
    await tick();
    expect(event.defaultPrevented).toBe(true);
    expect(box.checked).toBe(true); // Enter toggles instead
    expect(h.offered(9)).toBe(true);
    expect(h.fetchMock.mock.calls.some(([u]) => u.startsWith("/api/stock/"))).toBe(
      false,
    );
  });

  it("ignores a response whose shape isn't what the endpoint promises", async () => {
    const h = await open("take", (url) =>
      url.endsWith("/location-usage")
        ? Promise.resolve({ ok: true, json: async () => ({ holding: null }) })
        : Promise.resolve({ ok: true, json: async () => ({}) }),
    );
    expect(h.offered(9)).toBe(true); // unfiltered, not half-configured
    expect(h.document.querySelector(".loc-picker-toggle").disabled).toBe(false);
  });

  it("leaves the tree alone when the usage lookup fails", async () => {
    // A lookup for a convenience must never cost the user the write itself.
    const h = await open("add", () => Promise.reject(new Error("offline")));
    expect(h.offered(9)).toBe(true);
    expect(h.document.querySelector(".loc-picker-showall").hidden).toBe(true);
  });

  it("ignores a usage answer that lands after the dialog was reopened", async () => {
    // Reopening for a different component/mode while the first lookup is in flight
    // would otherwise filter the new dialog by the old component's stock.
    let resolveFirst;
    let call = 0;
    const fetchImpl = (url) => {
      if (!url.endsWith("/location-usage")) {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      call += 1;
      if (call === 1) return new Promise((resolve) => (resolveFirst = resolve));
      return Promise.resolve({
        ok: true,
        json: async () => ({ holding: [9], occupied: [5, 6, 9] }),
      });
    };
    const { window, document } = loadPage(stockPageFixture(nestedTree()), SCRIPTS, {
      fetchImpl,
    });
    const offered = (id) => {
      const node = document.querySelector(`.loc-picker-node[data-loc-id="${id}"]`);
      return !node.disabled && !node.closest("li").hidden;
    };

    window.openStockDialog("take", 7); // lookup hangs
    window.openStockDialog("take", 8); // reopened before it answers
    await tick();
    await tick();
    expect(offered(9)).toBe(true); // the SECOND component's stock

    resolveFirst({ ok: true, json: async () => ({ holding: [5], occupied: [5] }) });
    await tick();
    await tick();
    expect(offered(9)).toBe(true); // the stale answer changed nothing
    expect(offered(5)).toBe(false);
  });

  it("a location created inline stays pickable despite the filter", async () => {
    // A brand-new location can't hold this part yet, so a "holds it" filter would
    // hide the very location the user just deliberately created.
    // location_dialog.js must load BEFORE location_tree.js: the picker only
    // enables "+ New location" when window.openLocationDialog already exists.
    const fixture =
      stockPageFixture(nestedTree(), true) +
      `<dialog id="location-dialog"><form id="location-form">
         <select name="type"><option value="drawer">drawer</option></select>
         <input name="name" /><select name="parent_id"><option value=""></option></select>
         <p id="location-error" hidden></p><button type="submit"></button>
       </form></dialog>`;
    const created = { id: 77, name: "D77", parent_id: null, type: "drawer" };
    const fetchImpl = (url) => {
      if (url.endsWith("/location-usage")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ holding: [6], occupied: [6] }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => created });
    };
    const { window, document } = loadPage(fixture, [
      "shared.js",
      "location_dialog.js",
      "location_tree.js",
      "stock_dialog.js",
    ], { fetchImpl });

    window.openStockDialog("take", 7);
    await tick();
    await tick();
    // The filter is on: D9 is somebody else's, so it's gone.
    expect(
      document.querySelector('.loc-picker-node[data-loc-id="9"]').closest("li").hidden,
    ).toBe(true);

    // Create a location through the picker's inline button.
    document.querySelector(".loc-picker-new").click();
    document.querySelector('#location-form [name="name"]').value = "D77";
    document
      .getElementById("location-form")
      .dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
    await tick();
    await tick();

    const node = document.querySelector('.loc-picker-node[data-loc-id="77"]');
    expect(node).toBeTruthy();
    expect(node.disabled).toBe(false);
    expect(node.closest("li").hidden).toBe(false);
    // …and it was selected, so the user can submit straight away.
    expect(document.querySelector('[name="location_id"]').value).toBe("77");
  });
});

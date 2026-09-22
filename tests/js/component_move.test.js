import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, fetchBody } from "./harness.js";

const SCRIPTS = ["shared.js", "scan_putaway.js", "stock_move.js", "component_move.js"];

// A component's own page, trimmed to the move: the "Stock by location" table with
// a Move button per row, and the putaway dialog — WITHOUT the scan panel, which
// this page deliberately does not render (markup mirrors component_detail.html
// and templates/_putaway.html).
function detailMoveFixture() {
  return `
    <table class="data" id="stock-locations" data-component-id="42"
           data-label="RC0603" data-description="Yageo · 4k7 resistor">
      <tbody>
        <tr>
          <td>Lab / Rack A / D1</td><td class="num">100</td><td>loose</td>
          <td class="num">
            <button type="button" data-move-from="5" data-path="Lab / Rack A / D1"
                    data-quantity="100">Move</button>
          </td>
        </tr>
        <tr>
          <td>Lab / Shelf 02</td><td class="num">7</td><td>loose</td>
          <td class="num">
            <button type="button" data-move-from="9" data-path="Lab / Shelf 02"
                    data-quantity="7">Move</button>
          </td>
        </tr>
      </tbody>
    </table>
    <dialog id="putaway-dialog"
            data-locations='[{"id": 5, "path": "Lab / Rack A / D1"}, {"id": 9, "path": "Lab / Shelf 02"}]'>
      <strong id="putaway-title">Set location</strong>
      <form id="putaway-form">
        <p id="putaway-part"></p>
        <p id="putaway-desc"></p>
        <div class="field" id="putaway-from-field" hidden>
          <select id="putaway-from"></select>
        </div>
        <input id="putaway-qty" type="number" />
        <p id="putaway-qty-hint"></p>
        <input id="putaway-scan" readonly />
        <select id="putaway-select">
          <option value=""></option>
          <option value="5">D1</option>
          <option value="9">S2</option>
        </select>
        <p id="putaway-error" hidden></p>
        <button type="submit">Save</button>
      </form>
    </dialog>`;
}

// The harness stubs showModal/close at the prototype without touching `.open`,
// and this flow reads `.open` to decide where a scan goes. Make ours behave.
function syncDialogOpen(document, id) {
  const dialog = document.getElementById(id);
  dialog.showModal = vi.fn(() => {
    dialog.open = true;
  });
  dialog.close = vi.fn(() => {
    if (!dialog.open) return;
    dialog.open = false;
    dialog.dispatchEvent(new document.defaultView.Event("close"));
  });
  return dialog;
}

function press(document, key, target) {
  const event = new document.defaultView.KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
  });
  (target || document.body).dispatchEvent(event);
  return event;
}

// A wedge scanner: the payload, then its trailing Enter.
function scan(document, code) {
  for (const key of code) press(document, key);
  press(document, "Enter");
}

function open(fetchImpl) {
  const page = loadPage(detailMoveFixture(), SCRIPTS, { fetchImpl });
  page.dialog = syncDialogOpen(page.document, "putaway-dialog");
  page.move = (from = "5") =>
    page.document.querySelector(`[data-move-from="${from}"]`).click();
  page.moves = () =>
    page.fetchMock.mock.calls.filter(([u]) => u === "/api/stock/move");
  return page;
}

describe("component_move.js — moving a pile off its own page", () => {
  it("opens the putaway dialog on the row's own pile", () => {
    const page = open();

    page.move("9");

    const { document } = page;
    expect(document.getElementById("putaway-dialog").open).toBe(true);
    expect(document.getElementById("putaway-title").textContent).toBe("Move stock");
    expect(document.getElementById("putaway-part").textContent).toBe("RC0603");
    expect(document.getElementById("putaway-desc").textContent).toBe(
      "Yageo · 4k7 resistor",
    );
    // The row already answered "from where", so the source is named and settled,
    // and the count starts at the whole pile — a bag usually moves whole.
    const from = document.getElementById("putaway-from");
    expect(document.getElementById("putaway-from-field").hidden).toBe(false);
    expect([...from.options].map((o) => o.textContent)).toEqual([
      "Lab / Shelf 02 (7)",
    ]);
    expect(from.value).toBe("9");
    expect(document.getElementById("putaway-qty").value).toBe("7");
    expect(document.getElementById("putaway-qty").max).toBe("7");
    // And the destination starts empty: the one shelf it must not be is the one
    // the stock is leaving.
    expect(document.getElementById("putaway-select").value).toBe("");
  });

  it("files the move to the shelf that is scanned", async () => {
    const page = open();
    page.move("5");

    scan(page.document, "SL9");
    await tick();

    expect(page.moves()).toHaveLength(1);
    expect(fetchBody(page.fetchMock, 0)).toEqual({
      component_id: 42,
      from_location_id: 5,
      to_location_id: 9,
      quantity: 100,
    });
    expect(page.moves()[0][1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(page.dialog.open).toBe(false);
    // Both tables on this page were server-rendered, so only a reload can show
    // the move.
    expect(page.navigations).not.toHaveLength(0);
  });

  it("moves the count that was typed, not the whole pile", async () => {
    const page = open();
    page.move("5");
    page.document.getElementById("putaway-qty").value = "30";

    scan(page.document, "SL9");
    await tick();

    expect(fetchBody(page.fetchMock, 0).quantity).toBe(30);
  });

  it("refuses a move onto the shelf the stock is already on", async () => {
    const page = open();
    page.move("5");

    scan(page.document, "SL5");
    await tick();

    expect(page.moves()).toHaveLength(0);
    expect(page.document.getElementById("putaway-error").hidden).toBe(false);
    expect(page.document.getElementById("putaway-error").textContent).toContain(
      "Already in Lab / Rack A / D1",
    );
    // Still open, so the right shelf can be scanned without starting over.
    expect(page.dialog.open).toBe(true);
  });

  it("keeps the page's keyboard until the dialog is up", () => {
    // There is no panel here and nothing to scan an item with, so a page-level
    // collector between dialogs would swallow every keystroke on a page full of
    // links and headers — and this page has a search-your-own-way user on it.
    const page = open();

    expect(press(page.document, "S").defaultPrevented).toBe(false);

    page.move("5");
    // With the dialog up the keyboard is the scanner's again.
    expect(press(page.document, "S").defaultPrevented).toBe(true);
  });

  it("blocks a second move while the post-move reload is still pending", async () => {
    // A reload is a PENDING navigation: the page stays live and clickable until
    // the server answers, and the quantities on it are already history.
    const page = open();
    page.move("5");
    scan(page.document, "SL9");
    await tick();
    expect(page.moves()).toHaveLength(1);

    page.move("9");
    scan(page.document, "SL5");
    await tick();

    expect(page.moves()).toHaveLength(1);

    // …and a reload that never lands (Stop, a dropped connection, a bfcache
    // restore) must not leave the buttons dead for good.
    page.window.dispatchEvent(new page.window.Event("pageshow"));
    page.move("9");
    scan(page.document, "SL5");
    await tick();

    expect(page.moves()).toHaveLength(2);
  });

  it("says what the server refused, and keeps the dialog open", async () => {
    const page = open(() =>
      Promise.resolve({
        ok: false,
        json: () => Promise.resolve({ detail: "Only 3 left in Lab / Rack A / D1." }),
      }),
    );
    page.move("5");

    scan(page.document, "SL9");
    await tick();

    expect(page.document.getElementById("putaway-error").textContent).toBe(
      "Only 3 left in Lab / Rack A / D1.",
    );
    expect(page.dialog.open).toBe(true);
    expect(page.navigations).toHaveLength(0);
  });
});

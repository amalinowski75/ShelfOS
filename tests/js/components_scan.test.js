import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF, fetchBody } from "./harness.js";

const SCRIPTS = ["shared.js", "scan_putaway.js", "components_scan.js"];

// The components page's scan surface: the shared panel and dialog (markup from
// templates/_putaway.html), with no invoice tables in sight.
function componentsFixture() {
  return `
    <div id="components-table"></div>
    <div id="scan-panel"
         data-locations='[{"id": 5, "path": "Lab / Rack A / D1"}, {"id": 9, "path": "Lab / Shelf 02"}]'>
      <input id="scan-input" readonly />
      <p id="scan-status" class="scan-status"></p>
    </div>
    <dialog id="putaway-dialog">
      <form id="putaway-form">
        <p id="putaway-part"></p>
        <p id="putaway-desc"></p>
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

const ok = (data) => Promise.resolve({ ok: true, json: () => Promise.resolve(data) });

// One stocked component, as /api/components/scan answers for a real TME bag.
const MATCH = {
  identifiers: ["T821108A1S100CEU", "T821-1-08-S1"],
  matches: [
    {
      id: 42,
      mpn: "T821108A1S100CEU",
      manufacturer: "Amphenol",
      description: "IDC socket, 8 pin",
      locations: [{ id: 5, path: "Lab / Rack A / D1", quantity: 100 }],
    },
  ],
};

function routing(scanAnswer, moveAnswer) {
  return (url) => {
    if (url === "/api/components/scan") return ok(scanAnswer);
    return moveAnswer ?? ok({ id: 1, delta_quantity: 100 });
  };
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

function scan(document, code) {
  for (const key of code) press(document, key);
  press(document, "Enter");
}

function syncDialogOpen(document) {
  const dialog = document.getElementById("putaway-dialog");
  dialog.showModal.mockImplementation(() => {
    dialog.open = true;
  });
  dialog.close.mockImplementation(() => {
    dialog.open = false;
    dialog.dispatchEvent(new document.defaultView.Event("close"));
  });
}

async function openOn(answer, moveAnswer) {
  const page = loadPage(componentsFixture(), SCRIPTS, {
    fetchImpl: routing(answer, moveAnswer),
  });
  syncDialogOpen(page.document);
  scan(page.document, "QTY:100 PN:T821-1-08-S1 MPN:T821108A1S100CEU");
  await tick();
  return page;
}

describe("components_scan.js — resolving a bag", () => {
  it("looks the code up server-side and shows the component with its shelf", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/components/scan");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(fetchMock).code).toContain("MPN:T821108A1S100CEU");
    expect(document.getElementById("putaway-dialog").open).toBe(true);
    expect(document.getElementById("putaway-part").textContent).toBe(
      "T821108A1S100CEU",
    );
    expect(document.getElementById("putaway-desc").textContent).toBe(
      "Amphenol · IDC socket, 8 pin",
    );
    // The manual select starts on where the stock is now.
    expect(document.getElementById("putaway-select").value).toBe("5");
  });

  it("reports a code that matches nothing, naming what it read", async () => {
    const { document } = await openOn({ identifiers: ["NOPE-1"], matches: [] });
    const status = document.getElementById("scan-status");
    expect(status.className).toContain("error");
    expect(status.textContent).toBe("No component matches NOPE-1.");
    expect(document.getElementById("putaway-dialog").open).toBe(false);
  });

  it("refuses to guess when several components share the number", async () => {
    const { document } = await openOn({
      identifiers: ["SHARED-1"],
      matches: [
        { id: 1, mpn: "SHARED-1", manufacturer: null, description: null, locations: [] },
        { id: 2, mpn: "SHARED-1", manufacturer: null, description: null, locations: [] },
      ],
    });
    expect(document.getElementById("scan-status").textContent).toBe(
      "2 components share SHARED-1 — move it from its own page.",
    );
    expect(document.getElementById("putaway-dialog").open).toBe(false);
  });

  it("says so when the component has no stock to move", async () => {
    const { document } = await openOn({
      identifiers: ["EMPTY-1"],
      matches: [
        {
          id: 7,
          mpn: "EMPTY-1",
          manufacturer: null,
          description: null,
          locations: [],
        },
      ],
    });
    expect(document.getElementById("scan-status").textContent).toMatch(
      /EMPTY-1 has no stock recorded — use Add stock/,
    );
  });

  it("lists the places when stock is split, instead of picking one", async () => {
    const { document } = await openOn({
      identifiers: ["SPLIT-1"],
      matches: [
        {
          id: 8,
          mpn: "SPLIT-1",
          manufacturer: null,
          description: null,
          locations: [
            { id: 5, path: "Lab / Rack A / D1", quantity: 60 },
            { id: 9, path: "Lab / Shelf 02", quantity: 40 },
          ],
        },
      ],
    });
    const status = document.getElementById("scan-status").textContent;
    expect(status).toContain("stocked in several places");
    expect(status).toContain("Lab / Rack A / D1 (60)");
    expect(status).toContain("Lab / Shelf 02 (40)");
    expect(document.getElementById("putaway-dialog").open).toBe(false);
  });

  it("keeps the panel's height fixed whatever it says", () => {
    // Regression: the components table is sized to fit the whole page
    // (shared.js frameTable), so a panel that grows when a message appears
    // re-lays out the table — and a row button rebuilt between mousedown and
    // mouseup swallows the click. The status line is therefore always in flow
    // and never toggled hidden; only its text changes.
    const { document, window } = loadPage(componentsFixture(), SCRIPTS);
    const status = document.getElementById("scan-status");
    expect(status.hidden).toBe(false);
    expect(status.className).toContain("scan-status");

    window.dispatchEvent(new window.Event("blur")); // a long message arrives
    expect(status.hidden).toBe(false);
    expect(status.className).toContain("scan-status");
    window.dispatchEvent(new window.Event("focus")); // …and goes away again
    expect(status.hidden).toBe(false);
    expect(status.textContent).toBe("");
  });
});

describe("components_scan.js — moving the stock", () => {
  it("moves the whole slot to the scanned shelf and reports where it went", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    scan(document, "SL9");
    await tick();

    const [url, opts] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/stock/move");
    expect(opts.method).toBe("POST");
    // The whole slot by default — a bag changing drawer usually moves whole.
    expect(fetchBody(fetchMock, 1)).toEqual({
      component_id: 42,
      from_location_id: 5,
      to_location_id: 9,
      quantity: 100,
    });
    expect(document.getElementById("putaway-dialog").open).toBe(false);
    expect(document.querySelector(".toast-ok").textContent).toBe(
      "T821108A1S100CEU → Lab / Shelf 02",
    );
  });

  it("does not move anything when the scanned shelf is the current one", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    scan(document, "SL5"); // where it already is
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // the lookup only
    expect(document.getElementById("putaway-dialog").open).toBe(false);
    expect(document.querySelector(".toast-ok").textContent).toBe(
      "T821108A1S100CEU → Lab / Rack A / D1",
    );
  });

  it("keeps the dialog open and shows why when the move is refused", async () => {
    const { document } = await openOn(
      MATCH,
      Promise.resolve({
        ok: false,
        status: 422,
        json: () => Promise.resolve({ detail: "only 10 in stock at the source" }),
      }),
    );

    scan(document, "SL9");
    await tick();

    const error = document.getElementById("putaway-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("only 10 in stock");
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("refuses a location code this ShelfOS doesn't know", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    scan(document, "SL404");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // nothing was moved
    expect(document.getElementById("putaway-error").textContent).toContain("SL404");
  });

  it("moves to the manually picked shelf when a label is unreadable", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    document.getElementById("putaway-select").value = "9";
    document
      .getElementById("putaway-form")
      .dispatchEvent(
        new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
      );
    await tick();

    expect(fetchBody(fetchMock, 1)).toEqual({
      component_id: 42,
      from_location_id: 5,
      to_location_id: 9,
      quantity: 100,
    });
  });

  it("prefills the whole slot but moves only what the user typed", async () => {
    const { document, fetchMock } = await openOn(MATCH);
    const qty = document.getElementById("putaway-qty");
    expect(qty.value).toBe("100"); // the usual answer, ready for a scan-only flow
    expect(document.getElementById("putaway-qty-hint").textContent).toBe(
      "of 100 in Lab / Rack A / D1",
    );

    qty.value = "30";
    scan(document, "SL9");
    await tick();

    expect(fetchBody(fetchMock, 1).quantity).toBe(30);
  });

  it("refuses a quantity beyond what the source holds, before asking the server", async () => {
    const { document, fetchMock } = await openOn(MATCH);
    document.getElementById("putaway-qty").value = "101";

    scan(document, "SL9");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // nothing was moved
    expect(document.getElementById("putaway-error").textContent).toBe(
      "Only 100 available — cannot file 101.",
    );
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("refuses a blank or zero quantity", async () => {
    const { document, fetchMock } = await openOn(MATCH);
    for (const value of ["", "0", "2.5", "-3"]) {
      document.getElementById("putaway-qty").value = value;
      scan(document, "SL9");
      await tick();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(document.getElementById("putaway-error").textContent).toMatch(
        /whole number, 1 or more/,
      );
    }
  });

  it("hands the keyboard to the quantity box and takes it back on Enter", () => {
    const { document } = loadPage(componentsFixture(), SCRIPTS);
    const qty = document.getElementById("putaway-qty");
    qty.focus();
    // Typing a count is NOT collected as a scan…
    expect(press(document, "3", qty).defaultPrevented).toBe(false);
    expect(document.getElementById("scan-input").value).toBe("");
    expect(
      document.getElementById("scan-input").classList.contains("scan-armed"),
    ).toBe(false);

    // …and Enter leaves the field (without submitting) so the next scan lands.
    const enter = press(document, "Enter", qty);
    expect(enter.defaultPrevented).toBe(true);
    expect(document.activeElement).not.toBe(qty);
  });
});

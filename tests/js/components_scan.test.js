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
      <p id="scan-status" hidden></p>
    </div>
    <dialog id="putaway-dialog">
      <form id="putaway-form">
        <p id="putaway-part"></p>
        <p id="putaway-desc"></p>
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
    expect(status.className).toBe("error");
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
});

describe("components_scan.js — moving the stock", () => {
  it("moves the whole slot to the scanned shelf and reports where it went", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    scan(document, "SL9");
    await tick();

    const [url, opts] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/stock/move");
    expect(opts.method).toBe("POST");
    // No quantity: the whole slot moves, which is what a bag changing drawer is.
    expect(fetchBody(fetchMock, 1)).toEqual({
      component_id: 42,
      from_location_id: 5,
      to_location_id: 9,
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
    });
  });
});

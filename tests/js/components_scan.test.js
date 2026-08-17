import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, fetchBody } from "./harness.js";

// The harness stubs showModal/close at the prototype with bare vi.fn()s that
// never touch `.open`, so the two dialogs are indistinguishable by state. Give a
// dialog an OWN-property showModal that both records the call and flips `.open`,
// so a test can tell which dialog opened and how many times.
function trackOpen(dialog) {
  const spy = vi.fn(() => {
    dialog.open = true;
  });
  dialog.showModal = spy;
  return spy;
}

const SCRIPTS = ["shared.js", "scan_putaway.js", "components_scan.js"];
// With the New Component dialog too, for the "code matches nothing → create it"
// path, which reaches into openComponentDialog (component_dialog.js).
const SCRIPTS_WITH_DIALOG = [
  "shared.js",
  "component_dialog.js",
  "scan_putaway.js",
  "components_scan.js",
];

// The components page's scan surface: the shared panel and dialog (markup from
// templates/_putaway.html), with no invoice tables in sight. `withCreate` adds
// the New Component dialog (its import field is what a no-match scan drives).
function componentsFixture({ withCreate = false } = {}) {
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
    </dialog>
    ${withCreate ? createDialogMarkup() : ""}`;
}

// The New Component dialog, trimmed to what this flow touches: the import field
// and button (component_dialog.js wires them and exposes openComponentDialog).
function createDialogMarkup() {
  return `
    <dialog id="component-dialog"><form id="component-form">
      <input id="shop-import-url" type="text" />
      <button type="button" id="shop-import-btn"></button>
      <p id="shop-import-status" hidden></p>
      <select name="type_id" id="component-type">
        <option value="">Select a type…</option>
      </select>
      <button type="button" id="component-new-type" hidden></button>
      <input name="manufacturer" />
      <input name="mpn" />
      <input name="package" />
      <select name="mounting_type"><option value="Other" selected>Other</option></select>
      <input name="notes" />
      <p id="component-params-hint"></p>
      <div id="component-params"></div>
      <p id="component-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
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

function scan(document, code, { target } = {}) {
  for (const key of code) press(document, key, target);
  press(document, "Enter", target);
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

  it("opens the New Component dialog for a code that matches nothing, importing it", async () => {
    // A brand-new bag: nothing in inventory holds the code, so instead of a
    // dead-end miss the create dialog opens and looks the code up itself, so it
    // needn't be rescanned.
    const page = loadPage(componentsFixture({ withCreate: true }), SCRIPTS_WITH_DIALOG, {
      fetchImpl: (url) => {
        if (url === "/api/components/scan")
          return ok({ identifiers: ["NOPE-1"], matches: [] });
        if (url === "/api/shops/lookup")
          return ok({ category: "widget", mpn: "NOPE-1", description: "" });
        return ok({});
      },
    });
    // Own-property spies tell the two dialogs apart (the shared prototype mock
    // cannot): the create dialog opens, the putaway one is never touched.
    const createOpened = trackOpen(page.document.getElementById("component-dialog"));
    const putawayOpened = trackOpen(page.document.getElementById("putaway-dialog"));

    scan(page.document, "NOPE-1");
    await tick();

    expect(putawayOpened).not.toHaveBeenCalled();
    expect(createOpened).toHaveBeenCalledTimes(1);
    // A neutral toast keeps the diagnostic the old miss carried (naming what was
    // read), but it is not an error/warning — the flow succeeded.
    const toast = page.document.querySelector(".toast");
    expect(toast).toBeTruthy();
    expect(toast.textContent).toContain("NOPE-1");
    expect(page.document.querySelector(".toast-warn")).toBe(null);
    expect(page.document.getElementById("scan-status").className).not.toContain(
      "error",
    );
    // The create dialog ran the shop lookup with the scanned code, and the import
    // field carries it for the user to review.
    const lookup = page.fetchMock.mock.calls.find(
      ([url]) => url === "/api/shops/lookup",
    );
    expect(lookup).toBeTruthy();
    expect(JSON.parse(lookup[1].body).code).toBe("NOPE-1");
    expect(page.document.getElementById("shop-import-url").value).toBe("NOPE-1");
  });

  it("re-runs the lookup instead of reopening when a scan is queued behind the first", async () => {
    // A bag scanned while the first lookup is in flight is queued and drained
    // once the create dialog is already up. Reopening then would call showModal()
    // on an open dialog (an InvalidStateError in a real browser, surfacing as a
    // bogus network error) — so the second code must swap into the open dialog
    // and look up, not reopen it.
    let releaseFirst;
    const firstScanHeld = new Promise((r) => (releaseFirst = r));
    let scanCall = 0;
    const page = loadPage(componentsFixture({ withCreate: true }), SCRIPTS_WITH_DIALOG, {
      fetchImpl: (url) => {
        if (url === "/api/components/scan") {
          scanCall += 1;
          const answer = ok({ identifiers: [`C${scanCall}`], matches: [] });
          // Hold the FIRST lookup so the second scan is queued while the dialog
          // from the first is already open.
          return scanCall === 1 ? firstScanHeld.then(() => answer) : answer;
        }
        if (url === "/api/shops/lookup")
          return ok({ category: "widget", mpn: "X", description: "" });
        return ok({});
      },
    });
    const createOpened = trackOpen(page.document.getElementById("component-dialog"));

    scan(page.document, "AAA"); // busy — its components/scan is held
    await tick();
    scan(page.document, "BBB"); // queued behind it
    await tick();
    releaseFirst(); // first resolves → dialog opens → queue drains BBB
    await tick();
    await tick();

    // The dialog opened once (for AAA) and was NOT reopened for BBB…
    expect(createOpened).toHaveBeenCalledTimes(1);
    expect(page.document.querySelector(".toast-warn")).toBe(null); // no bogus error
    // …and BBB replaced AAA in the field and was itself looked up.
    expect(page.document.getElementById("shop-import-url").value).toBe("BBB");
    const lookups = page.fetchMock.mock.calls.filter(
      ([url]) => url === "/api/shops/lookup",
    );
    expect(lookups.map((c) => JSON.parse(c[1].body).code)).toEqual(["AAA", "BBB"]);
  });

  it("releases the in-flight import lock when a new code supersedes it", async () => {
    // A lookup abandoned by reopening must not wedge the next code out of ever
    // being looked up (the re-entrancy lock is module-scoped, not per session).
    let releaseLookup;
    const lookupHeld = new Promise((r) => (releaseLookup = r));
    let lookupCall = 0;
    const page = loadPage(componentsFixture({ withCreate: true }), SCRIPTS_WITH_DIALOG, {
      fetchImpl: (url) => {
        if (url === "/api/shops/lookup") {
          lookupCall += 1;
          const answer = ok({ category: "widget", mpn: "X", description: "" });
          return lookupCall === 1 ? lookupHeld.then(() => answer) : answer;
        }
        return ok({});
      },
    });
    trackOpen(page.document.getElementById("component-dialog"));

    // First open: its lookup hangs, so the lock would stay held without a reset.
    page.window.openComponentDialog(() => {}, null, { importCode: "AAA" });
    await tick();
    // Reopen with a new code while the first lookup is still in flight.
    page.window.openComponentDialog(() => {}, null, { importCode: "BBB" });
    await tick();

    const status = page.document.getElementById("shop-import-status");
    expect(status.textContent).not.toContain("Still looking"); // not refused
    expect(page.document.getElementById("shop-import-url").value).toBe("BBB");
    const lookups = page.fetchMock.mock.calls.filter(
      ([url]) => url === "/api/shops/lookup",
    );
    expect(lookups.map((c) => JSON.parse(c[1].body).code)).toEqual(["AAA", "BBB"]);
    releaseLookup();
    await tick();
  });

  it("falls back to a plain miss when the create dialog isn't on the page", async () => {
    // Without openComponentDialog (a page state that omits the create dialog),
    // a no-match code still reports rather than crashing.
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

  it("says nothing moved when the scanned shelf is the current one", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    scan(document, "SL5"); // where it already is
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // the lookup only
    // No green "moved" toast for a move that never happened.
    expect(document.querySelector(".toast-ok")).toBe(null);
    expect(document.getElementById("putaway-error").textContent).toBe(
      "Already in Lab / Rack A / D1 — nothing moved.",
    );
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("refuses the current shelf outright when a count was typed", async () => {
    const { document, fetchMock } = await openOn(MATCH);
    document.getElementById("putaway-qty").value = "30";

    scan(document, "SL5");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.getElementById("putaway-error").textContent).toMatch(
      /the 30 you typed went nowhere/,
    );
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("hands the scan out of the quantity box on its first letter", async () => {
    // The natural sequence: click the count, type it, scan the shelf. The
    // number input would silently drop "SL" and append "9" to the count.
    const { document, fetchMock } = await openOn(MATCH);
    const qty = document.getElementById("putaway-qty");
    qty.focus();
    qty.value = "30";

    scan(document, "SL9", { target: qty });
    await tick();

    expect(qty.value).toBe("30"); // untouched by the payload
    expect(fetchBody(fetchMock, 1)).toEqual({
      component_id: 42,
      from_location_id: 5,
      to_location_id: 9,
      quantity: 30,
    });
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

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
    </dialog>
    ${choiceDialogMarkup()}
    <dialog id="stock-dialog"><input /></dialog>
    ${withCreate ? createDialogMarkup() : ""}`;
}

// The what-next chooser (mirrors templates/_scan_choice.html): the three actions
// with their key hints, and the note that explains a greyed-out one.
function choiceDialogMarkup() {
  return `
    <dialog id="scan-choice-dialog">
      <article tabindex="-1" id="scan-choice-article">
        <button class="close" data-close></button>
        <p id="scan-choice-part"></p>
        <p id="scan-choice-desc"></p>
        <button type="button" data-choice="details"><kbd>D</kbd> Component details</button>
        <button type="button" data-choice="add"><kbd>A</kbd> Add stock</button>
        <button type="button" data-choice="move"><kbd>M</kbd> Move stock</button>
        <p id="scan-choice-note"></p>
      </article>
    </dialog>`;
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

// A different component, for the "one bag queued behind another" case.
const SECOND_BAG = {
  identifiers: ["SECOND-1"],
  matches: [
    {
      id: 77,
      mpn: "SECOND-1",
      manufacturer: null,
      description: null,
      locations: [{ id: 9, path: "Lab / Shelf 02", quantity: 5 }],
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

// The harness stubs showModal/close at the prototype without touching `.open`,
// but this flow now hands the screen from one dialog to the next and the code
// reads `.open` to decide who owns a scan. Make both of ours behave.
function syncDialogOpen(document, id) {
  const dialog = document.getElementById(id);
  // OWN properties, not mockImplementation: showModal/close are stubbed on the
  // shared prototype, so configuring them per dialog would have every dialog on
  // the page flipping the `.open` of whichever one was configured last — and this
  // flow has three of them handing the screen to each other.
  dialog.showModal = vi.fn(() => {
    dialog.open = true;
  });
  // `close` is fired from a QUEUED TASK, not inline. The shared stub in harness.js
  // dispatches it synchronously, which is a fine simplification until a script
  // sequences work around it — this one does: it closes the chooser and then opens
  // the next dialog, and whether the close handler runs before or after that is
  // the whole question. The spec says after, so say after here.
  dialog.close = vi.fn(() => {
    if (!dialog.open) return; // a real <dialog> ignores close() when it is shut
    dialog.open = false;
    setTimeout(
      () => dialog.dispatchEvent(new document.defaultView.Event("close")),
      0,
    );
  });
  return dialog;
}

// Scan a bag and stop where the page now stops: at the chooser.
async function openOn(answer, moveAnswer) {
  const page = loadPage(componentsFixture(), SCRIPTS, {
    fetchImpl: routing(answer, moveAnswer),
  });
  syncDialogOpen(page.document, "putaway-dialog");
  syncDialogOpen(page.document, "scan-choice-dialog");
  // stock_dialog.js isn't loaded here (its own suite covers it); the chooser only
  // needs to know an Add dialog exists, and tests assert what it was asked for.
  page.openStock = vi.fn();
  page.window.openStockDialog = page.openStock;
  scan(page.document, "QTY:100 PN:T821-1-08-S1 MPN:T821108A1S100CEU");
  await tick();
  return page;
}

// Answer the chooser by its key, the way the bench does.
function choose(page, key) {
  press(page.document, key, page.document.getElementById("scan-choice-article"));
}

// Scan a bag, then ask to move its stock — where a scan used to land directly.
async function movingOn(answer, moveAnswer) {
  const page = await openOn(answer, moveAnswer);
  choose(page, "m");
  return page;
}

describe("components_scan.js — resolving a bag", () => {
  it("looks the code up server-side and asks what the bag is for", async () => {
    const { document, fetchMock } = await openOn(MATCH);

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/components/scan");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(fetchMock).code).toContain("MPN:T821108A1S100CEU");
    // The chooser, not the move dialog: a scan no longer decides for the user.
    expect(document.getElementById("scan-choice-dialog").open).toBe(true);
    expect(document.getElementById("putaway-dialog").open).toBe(false);
    expect(document.getElementById("scan-choice-part").textContent).toBe(
      "T821108A1S100CEU",
    );
    expect(document.getElementById("scan-choice-desc").textContent).toBe(
      "Amphenol · IDC socket, 8 pin",
    );
    // All three are on offer for a component that has stock.
    for (const choice of ["details", "add", "move"]) {
      expect(
        document.querySelector(`[data-choice="${choice}"]`).disabled,
      ).toBe(false);
    }
  });

  it("opens the Add dialog on A, asking for the scanned component", async () => {
    const page = await openOn(MATCH);

    choose(page, "a");

    expect(page.openStock).toHaveBeenCalledTimes(1);
    const [mode, componentId] = page.openStock.mock.calls[0];
    expect(mode).toBe("add");
    expect(componentId).toBe(42);
    // The chooser gets out of the way — two stacked modals would trap the user.
    expect(page.document.getElementById("scan-choice-dialog").open).toBe(false);
    expect(page.document.getElementById("putaway-dialog").open).toBe(false);
  });

  it("refreshes the Qty column after an add, without reloading", async () => {
    // The table's Qty is a total, so an add changes it. A full reload would also
    // wipe the dialog's own confirmation, so the callback refreshes in place.
    const page = await openOn(MATCH);
    const refresh = vi.fn();
    page.window.loadTable = refresh;

    choose(page, "a");
    await page.openStock.mock.calls[0][2](); // the onSaved the dialog would run

    expect(refresh).toHaveBeenCalled();
  });

  it("opens the move dialog on M, and only then", async () => {
    const page = await openOn(MATCH);
    expect(page.document.getElementById("putaway-dialog").open).toBe(false);

    choose(page, "m");

    expect(page.document.getElementById("scan-choice-dialog").open).toBe(false);
    expect(page.document.getElementById("putaway-dialog").open).toBe(true);
    expect(page.document.getElementById("putaway-part").textContent).toBe(
      "T821108A1S100CEU",
    );
    // Named for the answer that opened it. "Set location" is the invoice flow's
    // wording; arriving here from "Move stock" and being told something else is
    // how a user starts doubting they pressed what they pressed.
    expect(page.document.getElementById("putaway-title").textContent).toBe(
      "Move stock",
    );
    // The destination starts EMPTY. Prefilling it with where the stock already
    // is would be a guess that reads as an answer — and the one shelf it must
    // not be is the one it is leaving.
    expect(page.document.getElementById("putaway-select").value).toBe("");
  });

  it("holds a bag scanned mid-lookup until the question is answered", async () => {
    // Two bags off the bench in quick succession: the second arrives while the
    // first is still being looked up, so it is queued. Draining it the moment the
    // first lookup lands would ask about bag two on top of the question about bag
    // one — two stacked choosers, the second silently replacing the first.
    let releaseFirst;
    const firstHeld = new Promise((r) => (releaseFirst = r));
    let call = 0;
    const page = loadPage(componentsFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (url !== "/api/components/scan") return ok({ id: 1 });
        call += 1;
        const answer = ok(call === 1 ? MATCH : SECOND_BAG);
        return call === 1 ? firstHeld.then(() => answer) : answer;
      },
    });
    syncDialogOpen(page.document, "putaway-dialog");
    const chooser = syncDialogOpen(page.document, "scan-choice-dialog");
    page.window.openStockDialog = vi.fn();

    scan(page.document, "BAG-ONE"); // its lookup is held
    await tick();
    scan(page.document, "BAG-TWO"); // queued behind it
    await tick();
    releaseFirst();
    await tick();
    await tick();

    // The question on screen is still about the first bag…
    expect(chooser.showModal).toHaveBeenCalledTimes(1);
    expect(page.document.getElementById("scan-choice-part").textContent).toBe(
      "T821108A1S100CEU",
    );

    // …and the second bag gets its turn once that question is done with.
    page.document.getElementById("scan-choice-dialog").close();
    await tick();
    await tick();

    expect(chooser.showModal).toHaveBeenCalledTimes(2);
    expect(page.document.getElementById("scan-choice-part").textContent).toBe(
      "SECOND-1",
    );
  });

  // Build a page with the first lookup held, so a second bag can be queued behind
  // the chooser the way it happens at the bench.
  async function queuedBehindChooser() {
    let releaseFirst;
    const firstHeld = new Promise((r) => (releaseFirst = r));
    let call = 0;
    const page = loadPage(componentsFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (url !== "/api/components/scan") return ok({ id: 1 });
        call += 1;
        const answer = ok(call === 1 ? MATCH : SECOND_BAG);
        return call === 1 ? firstHeld.then(() => answer) : answer;
      },
    });
    syncDialogOpen(page.document, "putaway-dialog");
    syncDialogOpen(page.document, "scan-choice-dialog");
    const stock = syncDialogOpen(page.document, "stock-dialog");
    // Open it for real, the way the dialog it stands in for would.
    page.openStock = vi.fn(() => stock.showModal());
    page.window.openStockDialog = page.openStock;
    page.stock = stock;
    page.lookups = () =>
      page.fetchMock.mock.calls.filter(([u]) => u === "/api/components/scan").length;

    scan(page.document, "BAG-ONE");
    await tick();
    scan(page.document, "BAG-TWO"); // queued
    await tick();
    releaseFirst();
    await tick();
    await tick();
    return page;
  }

  it("holds the queued bag while the answer's own dialog is up", async () => {
    // Answering does not release the queue — the answer does, when it is done.
    // Draining on the chooser's close would resolve bag two into a lookup (and a
    // second chooser) behind the Add dialog the user is standing in.
    const page = await queuedBehindChooser();
    expect(page.lookups()).toBe(1);

    choose(page, "a");
    await tick();

    expect(page.openStock).toHaveBeenCalledTimes(1);
    expect(page.lookups()).toBe(1); // bag two still waiting

    page.stock.close(); // the Add dialog is done with the screen
    await tick();
    await tick();

    expect(page.lookups()).toBe(2);
    expect(page.document.getElementById("scan-choice-part").textContent).toBe(
      "SECOND-1",
    );
  });

  it("drops the queued bag when the answer is leaving the page", async () => {
    // Details navigates away. Nothing will close after it, so there is no "done"
    // to wait for — and resolving bag two in the moment before the page goes buys
    // a lookup and a flash of a second chooser that nobody will ever act on.
    const page = await queuedBehindChooser();

    choose(page, "d");
    await tick();
    await tick();

    expect(page.lookups()).toBe(1);
  });

  it("answers to the buttons as well as the keys", async () => {
    const page = await openOn(MATCH);

    page.document.querySelector('[data-choice="move"]').click();

    expect(page.document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("ignores the scanner's trailing Enter instead of answering with it", async () => {
    // The chooser opens the instant a scan lands, and a wedge scanner ends its
    // payload with Enter — arriving at whatever showModal focused. Answering the
    // question with the terminator of the scan that asked it is the one thing
    // this dialog must never do.
    const page = await openOn(MATCH);
    const article = page.document.getElementById("scan-choice-article");
    article.focus();

    const enter = press(page.document, "Enter", article);
    const space = press(page.document, " ", article);

    expect(enter.defaultPrevented).toBe(true);
    expect(space.defaultPrevented).toBe(true);
    expect(page.document.getElementById("scan-choice-dialog").open).toBe(true);
    expect(page.document.getElementById("putaway-dialog").open).toBe(false);
    expect(page.openStock).not.toHaveBeenCalled();
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
      "2 components share SHARED-1 — open one from the table.",
    );
    expect(document.getElementById("putaway-dialog").open).toBe(false);
    expect(document.getElementById("scan-choice-dialog").open).toBe(false);
  });

  it("greys out Move for a component with no stock, and says why", async () => {
    const page = await openOn({
      identifiers: ["EMPTY-1"],
      matches: [
        { id: 7, mpn: "EMPTY-1", manufacturer: null, description: null, locations: [] },
      ],
    });
    const { document } = page;

    expect(document.getElementById("scan-choice-dialog").open).toBe(true);
    expect(document.querySelector('[data-choice="move"]').disabled).toBe(true);
    // Disabled with no explanation is just a dead button; the note carries the why.
    expect(document.getElementById("scan-choice-note").textContent).toMatch(
      /no stock on record/i,
    );
    // The two that still make sense are still offered.
    expect(document.querySelector('[data-choice="add"]').disabled).toBe(false);
    expect(document.querySelector('[data-choice="details"]').disabled).toBe(false);
  });

  it("does not answer M when Move is greyed out", async () => {
    // The key has to obey the same rule as the button, or the keyboard would
    // reach a flow the mouse cannot.
    const page = await openOn({
      identifiers: ["EMPTY-1"],
      matches: [
        { id: 7, mpn: "EMPTY-1", manufacturer: null, description: null, locations: [] },
      ],
    });

    choose(page, "m");

    expect(page.document.getElementById("putaway-dialog").open).toBe(false);
    expect(page.document.getElementById("scan-choice-dialog").open).toBe(true);
  });

  it("stands down while another dialog on the page is open", () => {
    // The stock dialog (and the New Component dialog) live on this same page and
    // take their own scans. While one is open the putaway collector must stay
    // silent, wherever focus rests — else a shelf label scanned into that modal
    // leaks to this panel behind it. Focus on <body> (a click on the modal's
    // chrome) is the case a focus-only check misses.
    const { document } = loadPage(
      componentsFixture() + `<dialog id="stock-dialog"><input /></dialog>`,
      SCRIPTS,
    );
    document.getElementById("stock-dialog").setAttribute("open", ""); // a foreign modal

    press(document, "S");
    press(document, "L");

    // Nothing was collected into the putaway panel's field.
    expect(document.getElementById("scan-input").value).toBe("");
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
    const { document, fetchMock } = await movingOn(MATCH);

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
    const { document, fetchMock } = await movingOn(MATCH);

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
    const { document, fetchMock } = await movingOn(MATCH);
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
    const { document, fetchMock } = await movingOn(MATCH);
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
    const { document } = await movingOn(
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
    const { document, fetchMock } = await movingOn(MATCH);

    scan(document, "SL404");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // nothing was moved
    expect(document.getElementById("putaway-error").textContent).toContain("SL404");
  });

  it("moves to the manually picked shelf when a label is unreadable", async () => {
    const { document, fetchMock } = await movingOn(MATCH);

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
    const { document, fetchMock } = await movingOn(MATCH);
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
    const { document, fetchMock } = await movingOn(MATCH);
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
    const { document, fetchMock } = await movingOn(MATCH);
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

// Stock split across two shelves. Before, a scan refused this outright ("move it
// from its own page"); the source picker is what turns it into an ordinary move.
const SPLIT = {
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
};

describe("components_scan.js — where the stock comes from", () => {
  it("preselects the only pile there is, and still shows which", async () => {
    const { document } = await movingOn(MATCH);
    const from = document.getElementById("putaway-from");

    // Shown even with nothing to decide: the point of the field is that you can
    // read where the stock is leaving from before committing to a move. With one
    // pile there is simply no empty option to pick past.
    expect(document.getElementById("putaway-from-field").hidden).toBe(false);
    expect([...from.options].map((o) => o.textContent)).toEqual([
      "Lab / Rack A / D1 (100)",
    ]);
    expect(from.value).toBe("5");
    expect(document.getElementById("putaway-qty").value).toBe("100");
  });

  it("offers every pile, with its count, and picks none of them", async () => {
    const { document } = await movingOn(SPLIT);
    const from = document.getElementById("putaway-from");

    expect(document.getElementById("putaway-from-field").hidden).toBe(false);
    expect([...from.options].map((o) => o.textContent)).toEqual([
      "— choose a source —",
      "Lab / Rack A / D1 (60)",
      "Lab / Shelf 02 (40)",
    ]);
    expect(from.value).toBe("");
    // "How many" has no meaning before "out of which", so the box stays empty
    // and says what it is waiting for.
    expect(document.getElementById("putaway-qty").value).toBe("");
    expect(document.getElementById("putaway-qty-hint").textContent).toMatch(
      /pick where the stock comes from/i,
    );
  });

  it("fills the count from whichever pile is picked, and caps it there", async () => {
    const { document } = await movingOn(SPLIT);
    const from = document.getElementById("putaway-from");

    from.value = "9";
    from.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));

    const qty = document.getElementById("putaway-qty");
    expect(qty.value).toBe("40"); // the whole of THAT pile, not the other one
    expect(qty.max).toBe("40");
    expect(document.getElementById("putaway-qty-hint").textContent).toBe(
      "of 40 in Lab / Shelf 02",
    );
  });

  it("moves out of the picked pile, not the first one on the list", async () => {
    const { document, fetchMock } = await movingOn(SPLIT);
    const from = document.getElementById("putaway-from");
    from.value = "9";
    from.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));

    scan(document, "SL5");
    await tick();

    expect(fetchBody(fetchMock, 1)).toEqual({
      component_id: 8,
      from_location_id: 9,
      to_location_id: 5,
      quantity: 40,
    });
  });

  it("asks which pile before moving anything", async () => {
    const { document, fetchMock } = await movingOn(SPLIT);

    scan(document, "SL5"); // a destination, but no source chosen yet
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // the lookup only
    expect(document.getElementById("putaway-error").textContent).toBe(
      "Choose where the stock comes from.",
    );
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("holds the count to the picked pile, not the biggest one", async () => {
    // 60 is on the shelf next door, and the server would refuse it — but the
    // dialog knows enough to say so first.
    const { document, fetchMock } = await movingOn(SPLIT);
    const from = document.getElementById("putaway-from");
    from.value = "9";
    from.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    document.getElementById("putaway-qty").value = "60";

    scan(document, "SL5");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.getElementById("putaway-error").textContent).toBe(
      "Only 40 available — cannot file 60.",
    );
  });
});

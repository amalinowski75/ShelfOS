import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF, fetchBody } from "./harness.js";

const SCRIPTS = ["shared.js", "invoice_scan.js"];

// The draft-invoice putaway surface: scan panel, one staged import row, one
// regular line row, and the putaway dialog (markup mirrors invoice_detail.html).
function scanFixture({ pendingLocation = "" } = {}) {
  const selected = (v) => (pendingLocation === v ? "selected" : "");
  return `
    <div id="invoice-detail" data-invoice-id="7"></div>
    <div id="invoice-scan"
         data-locations='[{"id": 5, "path": "Lab / Rack A / D1"}, {"id": 9, "path": "Lab / Shelf 02"}]'>
      <input id="invoice-scan-input" readonly />
      <p id="invoice-scan-status" hidden></p>
    </div>
    <table id="invoice-review"><tbody>
      <tr data-import-line-id="21" class="is-incomplete" data-type-id="3"
          data-mpn="ABC123" data-spn="71-ABC123" data-description="A widget">
        <td><span class="mono">ABC123</span></td>
        <td>
          <select class="ril-location">
            <option value=""></option>
            <option value="5" ${selected("5")}>D1</option>
            <option value="9" ${selected("9")}>S2</option>
          </select>
        </td>
      </tr>
    </tbody></table>
    <table id="invoice-lines"><tbody>
      <tr data-line-id="31" data-spn="SPN-9" data-mpn="R-100" data-location-id="">
        <td><a>R-100</a></td>
        <td class="mono">SPN-9</td>
        <td>—</td>
      </tr>
    </tbody></table>
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

function parseRouting(parsed) {
  return (url) => {
    if (url === "/api/shops/parse") return ok(parsed);
    return ok({});
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

// A wedge scanner is a fast keyboard: emit the payload as per-key keydown
// events on the given target (the body by default — focus must not matter),
// terminated with Enter. Dispatched synchronously, so the collector sees them
// as one burst. The collector listens on the document, capture phase.
function scan(document, code, { target } = {}) {
  for (const key of code) press(document, key, target);
  return press(document, "Enter", target);
}

// jsdom's showModal is a stub, so reflect the state the browser would set.
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

describe("invoice_scan.js — bag scan", () => {
  it("collects a scan typed with focus anywhere and opens the dialog on the match", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
    });
    syncDialogOpen(document);

    scan(document, "[)>x1PABC123x");
    // The buffer is mirrored into the (readonly) display field as it grows —
    // checked before Enter fires via a fresh partial scan below.
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/shops/parse");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(fetchMock)).toEqual({ code: "[)>x1PABC123x" });
    const dialog = document.getElementById("putaway-dialog");
    expect(dialog.showModal).toHaveBeenCalledTimes(1);
    expect(dialog.open).toBe(true);
    expect(document.getElementById("putaway-part").textContent).toBe("ABC123");
    expect(document.getElementById("putaway-desc").textContent).toBe("A widget");
    expect(document.getElementById("invoice-scan-input").value).toBe("");
  });

  it("mirrors the buffer into the display field and honours Backspace", () => {
    const { document } = loadPage(scanFixture(), SCRIPTS);
    const field = document.getElementById("invoice-scan-input");
    for (const key of ["A", "B", "C"]) {
      document.body.dispatchEvent(
        new document.defaultView.KeyboardEvent("keydown", {
          key,
          bubbles: true,
          cancelable: true,
        }),
      );
    }
    expect(field.value).toBe("ABC");
    document.body.dispatchEvent(
      new document.defaultView.KeyboardEvent("keydown", {
        key: "Backspace",
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(field.value).toBe("AB");
    expect(field.classList.contains("scan-armed")).toBe(true);
  });

  it("collects keystrokes wherever focus rests, short of a page control", async () => {
    // The field-tested failure: focus parked somewhere unreachable (body after
    // a dialog closes, or eaten by a password-manager overlay). The collector
    // must not care — only a real page control makes it stand down.
    const { document } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
    });
    syncDialogOpen(document);
    const div = document.createElement("div");
    document.body.appendChild(div);

    scan(document, "bagcode", { target: div });
    await tick();

    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("prefers a row that still lacks a location when the same part appears twice", async () => {
    // The staged row already has a location; the regular line (same MPN) has
    // none — the scan must walk on to the line, not reopen the finished row.
    const fixture = scanFixture({ pendingLocation: "5" }).replace(
      'data-mpn="R-100"',
      'data-mpn="ABC123"',
    );
    const { document } = loadPage(fixture, SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
    });
    syncDialogOpen(document);

    scan(document, "bag");
    await tick();
    scan(document, "SL9");
    await tick();

    // Saved to the LINE endpoint — the location-less match won.
    const row = document.querySelector("#invoice-lines tr");
    expect(row.dataset.locationId).toBe("9");
  });

  it("matches a staged row by the supplier part number (a TME bag's PN)", async () => {
    // A TME QR yields the ordering symbol as `mpn`; on the invoice that value
    // lives in supplier_part_number, not the manufacturer part number.
    const { document } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "71-ABC123", distributor_pn: null }),
    });
    syncDialogOpen(document);

    scan(document, "QTY:5 PN:71-ABC123");
    await tick();

    expect(document.getElementById("putaway-dialog").open).toBe(true);
    expect(document.getElementById("putaway-part").textContent).toBe("ABC123");
  });

  it("falls back to the URL's symbols when the code carries no part number", async () => {
    // A URL-only QR: the path's symbol is the only identifier there is.
    const { document } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({
        mpn: null,
        distributor_pn: null,
        url: "https://www.tme.eu/pl/details/71-abc123/zlacza/acme/",
        url_symbols: ["71-ABC123", "ZLACZA", "ACME"],
      }),
    });
    syncDialogOpen(document);

    scan(document, "https://www.tme.eu/pl/details/71-abc123/zlacza/acme/");
    await tick();

    expect(document.getElementById("putaway-part").textContent).toBe("ABC123");
  });

  it("ignores URL symbols when the code has a real part number", async () => {
    // The path's category/manufacturer segments must never compete with an
    // identifier the label states outright — that could file the wrong bag.
    const { document } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({
        mpn: "NOT-ON-THIS-INVOICE",
        distributor_pn: null,
        url_symbols: ["71-ABC123"], // would have matched the staged row
      }),
    });
    syncDialogOpen(document);

    scan(document, "bag");
    await tick();

    expect(document.getElementById("putaway-dialog").open).toBe(false);
    expect(document.getElementById("invoice-scan-status").textContent).toContain(
      "NOT-ON-THIS-INVOICE",
    );
  });

  it("matches a regular line by the distributor part number", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "NOPE-1", distributor_pn: "SPN-9" }),
    });
    syncDialogOpen(document);

    scan(document, "bag");
    await tick();
    scan(document, "sl9"); // case-insensitive
    await tick();

    const save = fetchMock.mock.calls[1];
    expect(save[0]).toBe("/api/invoices/7/lines/31/location");
    expect(save[1].method).toBe("PUT");
    expect(fetchBody(fetchMock, 1)).toEqual({ location_id: 9 });
    const row = document.querySelector("#invoice-lines tr");
    expect(row.dataset.locationId).toBe("9");
    expect(row.children[2].textContent).toBe("Lab / Shelf 02");
    expect(document.getElementById("putaway-dialog").open).toBe(false);
  });

  it("rejects a location label scanned into the bag field", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS);
    scan(document, "SL5");
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    const status = document.getElementById("invoice-scan-status");
    expect(status.hidden).toBe(false);
    expect(status.className).toBe("error");
    expect(status.textContent).toMatch(/location label/);
  });

  it("reports a bag that matches no line", async () => {
    const { document } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "UNKNOWN-99", distributor_pn: null }),
    });
    scan(document, "bag");
    await tick();

    const status = document.getElementById("invoice-scan-status");
    expect(status.className).toBe("error");
    expect(status.textContent).toContain("UNKNOWN-99");
    expect(document.getElementById("putaway-dialog").showModal).not.toHaveBeenCalled();
  });

  it("queues a scan that arrives mid-parse instead of dropping it", async () => {
    // The silent-loss report: bag B scanned while bag A's parse is in flight
    // vanished without a trace. It must wait its turn and then run.
    const resolvers = [];
    const codes = [];
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: (url, opts) => {
        codes.push(JSON.parse(opts.body).code);
        return new Promise((resolve) => {
          resolvers.push(() =>
            resolve({
              ok: true,
              json: () => Promise.resolve({ mpn: "UNKNOWN-1", distributor_pn: null }),
            }),
          );
        });
      },
    });
    syncDialogOpen(document);

    scan(document, "AAA");
    scan(document, "BBB"); // previous parse still pending
    const status = document.getElementById("invoice-scan-status");
    expect(status.textContent).toMatch(/finishing the previous scan/);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolvers[0]();
    await tick();
    resolvers[1]?.();
    await tick();
    expect(codes).toEqual(["AAA", "BBB"]);
  });

  it("runs a queued scan after the dialog closes", async () => {
    // Bag B scanned a beat too early, while A's parse was about to open the
    // dialog: B holds until the dialog closes, then gets its turn.
    const resolvers = [];
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: (url, opts) =>
        new Promise((resolve) => {
          resolvers.push(() =>
            resolve({
              ok: true,
              json: () => Promise.resolve({ mpn: "ABC123", distributor_pn: null }),
            }),
          );
        }),
    });
    syncDialogOpen(document);

    scan(document, "bagA");
    scan(document, "bagB"); // queued behind A
    resolvers[0]();
    await tick();
    const dialog = document.getElementById("putaway-dialog");
    expect(dialog.open).toBe(true); // A's dialog is up; B still waiting
    expect(fetchMock).toHaveBeenCalledTimes(1);

    dialog.close(); // user cancels A (Escape/Cancel)
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(2); // B fired on its own
  });

  it("warns while the window is unfocused and clears the warning on return", () => {
    const { window, document } = loadPage(scanFixture(), SCRIPTS);
    window.dispatchEvent(new window.Event("blur"));
    const status = document.getElementById("invoice-scan-status");
    expect(status.hidden).toBe(false);
    expect(status.textContent).toMatch(/not focused/);

    window.dispatchEvent(new window.Event("focus"));
    expect(status.hidden).toBe(true);
  });

  it("restores a real status message after an alt-tab round trip", async () => {
    const { window, document } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "UNKNOWN-99", distributor_pn: null }),
    });
    scan(document, "bag");
    await tick();
    const status = document.getElementById("invoice-scan-status");
    expect(status.textContent).toContain("UNKNOWN-99");

    window.dispatchEvent(new window.Event("blur"));
    expect(status.textContent).toMatch(/not focused/);
    window.dispatchEvent(new window.Event("focus"));
    // The unacted-on error is back, not silently swallowed by the warning.
    expect(status.textContent).toContain("UNKNOWN-99");
    expect(status.hidden).toBe(false);
  });

  it("assembles a payload whose keys arrive with uneven gaps", async () => {
    // Regression: real scanners stall mid-payload (HID polling, a layout
    // switch for ":" or "/"). A gap must never be read as "a new scan" and
    // truncate the code — the field cleared three times mid-scan and the
    // server got a fragment.
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
    });
    syncDialogOpen(document);
    const field = document.getElementById("invoice-scan-input");

    for (const key of "PN:ABC") {
      press(document, key);
      await new Promise((resolve) => setTimeout(resolve, 90)); // slow scanner
    }
    for (const key of "123") press(document, key); // then a fast run
    expect(field.value).toBe("PN:ABC123"); // nothing was dropped
    press(document, "Enter");
    await tick();

    expect(fetchBody(fetchMock)).toEqual({ code: "PN:ABC123" });
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("hands a focused page control its whole keyboard, type-ahead run included", () => {
    // Type-ahead is typed FAST ("lab" to reach "Lab / …"), so every character
    // after the first must reach the select too — the case a per-keystroke
    // speed rule could never get right.
    const { document } = loadPage(scanFixture(), SCRIPTS);
    const rowSelect = document.querySelector(".ril-location");
    rowSelect.focus();
    for (const key of "lab") {
      expect(press(document, key, rowSelect).defaultPrevented).toBe(false);
    }
    // Space activates a focused button, and Enter still submits.
    const button = document.createElement("button");
    document.body.appendChild(button);
    button.focus();
    expect(press(document, " ", button).defaultPrevented).toBe(false);
    expect(press(document, "Enter", button).defaultPrevented).toBe(false);
    // None of it was collected, and the panel says the keyboard is elsewhere.
    expect(document.getElementById("invoice-scan-input").value).toBe("");
    expect(document.getElementById("invoice-scan-status").textContent).toMatch(
      /Keyboard is with the page/,
    );
    expect(
      document.getElementById("invoice-scan-input").classList.contains("scan-armed"),
    ).toBe(false);
  });

  it("keeps a scan clean when a page keystroke came moments earlier", async () => {
    // The bench sequence: set a row's location from the keyboard, then scan
    // the next bag. Nothing typed at the control may prefix the code.
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
    });
    syncDialogOpen(document);
    const rowSelect = document.querySelector(".ril-location");
    rowSelect.focus();
    press(document, "l", rowSelect);
    rowSelect.blur(); // the user moves on; scanning resumes

    scan(document, "PN:ABC123");
    await tick();
    expect(fetchBody(fetchMock)).toEqual({ code: "PN:ABC123" });
  });

  it("gives the keyboard back on Escape, without the mouse", () => {
    const { document } = loadPage(scanFixture(), SCRIPTS);
    const rowSelect = document.querySelector(".ril-location");
    rowSelect.focus();
    expect(document.getElementById("invoice-scan-status").textContent).toMatch(
      /Keyboard is with the page/,
    );

    press(document, "Escape", rowSelect);
    expect(document.activeElement).not.toBe(rowSelect);
    expect(
      document.getElementById("invoice-scan-input").classList.contains("scan-armed"),
    ).toBe(true);
    // …and the next scan is collected again.
    press(document, "X");
    expect(document.getElementById("invoice-scan-input").value).toBe("X");
  });

  it("drops an unterminated buffer so strays can't prefix the next scan", async () => {
    const { document } = loadPage(scanFixture(), SCRIPTS);
    press(document, "L"); // focus is on the body: this IS collected
    const field = document.getElementById("invoice-scan-input");
    expect(field.value).toBe("L");

    await new Promise((resolve) => setTimeout(resolve, 1300)); // idle window
    expect(field.value).toBe("");
  });

  it("consumes a payload aimed at the dialog's own focused button", async () => {
    // showModal focuses the dialog's close button, and this is where the
    // location scan lands. Every character — Space included — must be
    // consumed there, or the payload would click the button and the dialog
    // would vanish mid-scan.
    const { document } = await (async () => {
      const page = loadPage(scanFixture(), SCRIPTS, {
        fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
      });
      syncDialogOpen(page.document);
      scan(page.document, "bag");
      await tick();
      return page;
    })();

    const close = document.createElement("button");
    document.getElementById("putaway-dialog").appendChild(close);
    close.focus();
    expect(press(document, " ", close).defaultPrevented).toBe(true);
    scan(document, "SL5", { target: close });
    await tick();

    expect(document.querySelector(".ril-location").value).toBe("5");
  });

  it("leaves Enter alone when there is nothing to submit", () => {
    // With an empty buffer, Enter belongs to the page (forms, buttons) —
    // except inside the dialog, where a scanner's extra terminator would
    // click the focused close button.
    const { document } = loadPage(scanFixture(), SCRIPTS);
    expect(press(document, "Enter").defaultPrevented).toBe(false);
    document.getElementById("putaway-dialog").open = true;
    expect(press(document, "Enter").defaultPrevented).toBe(true);
  });

  it("leaves other dialogs' typing alone", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS);
    document.body.insertAdjacentHTML(
      "beforeend",
      '<dialog id="other-dialog" open><input id="other-input" /></dialog>',
    );
    const other = document.getElementById("other-input");
    other.focus();
    scan(document, "typing", { target: other });
    await tick();

    // Nothing collected, nothing parsed.
    expect(document.getElementById("invoice-scan-input").value).toBe("");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("invoice_scan.js — location scan in the dialog", () => {
  async function openOnStagedRow(overrides = {}) {
    const page = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
      ...overrides,
    });
    syncDialogOpen(page.document);
    scan(page.document, "bag");
    await tick();
    return page;
  }

  it("saves a scanned SL code to the import line and updates the row in place", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    scan(document, "SL5");
    await tick();

    const save = fetchMock.mock.calls[1];
    expect(save[0]).toBe("/api/invoices/7/import-lines/21");
    expect(save[1].method).toBe("PATCH");
    expect(fetchBody(fetchMock, 1)).toEqual({ location_id: 5 });
    const row = document.querySelector("#invoice-review tr");
    expect(row.querySelector(".ril-location").value).toBe("5");
    expect(row.classList.contains("is-incomplete")).toBe(false); // type was set
    expect(document.getElementById("putaway-dialog").open).toBe(false);
    // A toast names the part and the human-readable path it went to.
    expect(document.querySelector(".toast-ok").textContent).toBe(
      "ABC123 → Lab / Rack A / D1",
    );
    // The collector is armed for the next bag again.
    expect(
      document
        .getElementById("invoice-scan-input")
        .classList.contains("scan-armed"),
    ).toBe(true);
  });

  it("collects the location scan while the dialog is open, wherever focus is", async () => {
    const { document } = await openOnStagedRow();

    // Keystrokes land on the dialog's select (where a browser may park focus);
    // the collector still owns them — the select is mouse-operated by design.
    const select = document.getElementById("putaway-select");
    select.focus();
    scan(document, "SL5", { target: select });
    await tick();

    expect(document.getElementById("putaway-dialog").open).toBe(false);
    expect(document.querySelector(".ril-location").value).toBe("5");
  });

  it("swallows a scanner's stray extra terminator while the dialog is open", async () => {
    // CR+LF-suffixed scanners fire a second Enter right after the code's own;
    // with the dialog freshly open and its close button focused, an unhandled
    // Enter would "click" it and the dialog would vanish unseen.
    const { document } = await openOnStagedRow();
    const stray = new document.defaultView.KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    document.body.dispatchEvent(stray);
    expect(stray.defaultPrevented).toBe(true); // buffer empty, dialog open
    expect(document.getElementById("putaway-dialog").open).toBe(true);

    // With no dialog up, a plain Enter stays untouched (forms, buttons).
    document.getElementById("putaway-dialog").close();
    const plain = new document.defaultView.KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    document.body.dispatchEvent(plain);
    expect(plain.defaultPrevented).toBe(false);
  });

  it("still files the row when the dialog is cancelled mid-save", async () => {
    // The close listener nulls the shared target; a save resolving afterwards
    // must still apply the row the server actually recorded, and must not
    // report a phantom network error.
    let resolveSave;
    const { document, fetchMock } = await openOnStagedRow({
      fetchImpl: (url) =>
        url === "/api/shops/parse"
          ? ok({ mpn: "ABC123", distributor_pn: null })
          : new Promise((resolve) => {
              resolveSave = () => resolve({ ok: true, json: () => Promise.resolve({}) });
            }),
    });

    scan(document, "SL5");
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(2); // save in flight
    document.getElementById("putaway-dialog").close(); // user hits Escape
    await tick();
    resolveSave();
    await tick();

    expect(document.querySelector(".ril-location").value).toBe("5");
    expect(document.getElementById("putaway-error").textContent).not.toMatch(
      /Could not reach/,
    );
    expect(document.querySelector(".toast-ok").textContent).toBe(
      "ABC123 → Lab / Rack A / D1",
    );
  });

  it("tells the user when a location scan lands during a save", async () => {
    const { document, fetchMock } = await openOnStagedRow({
      fetchImpl: (url) =>
        url === "/api/shops/parse"
          ? ok({ mpn: "ABC123", distributor_pn: null })
          : new Promise(() => {}), // never settles: the save is in flight
    });

    scan(document, "SL5");
    await tick();
    scan(document, "SL9"); // re-scan while the first save hangs
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(2); // the second scan didn't fire
    const error = document.getElementById("putaway-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/Still saving/);
  });

  it("keeps a genuine dialog error across an alt-tab round trip", async () => {
    const { window, document } = await openOnStagedRow();
    scan(document, "SL777"); // unknown location
    await tick();
    const error = document.getElementById("putaway-error");
    expect(error.textContent).toContain("SL777");

    window.dispatchEvent(new window.Event("blur"));
    expect(error.textContent).toMatch(/not focused/);
    window.dispatchEvent(new window.Event("focus"));
    expect(error.textContent).toContain("SL777");
    expect(error.hidden).toBe(false);
  });

  it("refuses a location code that is not on this ShelfOS", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    scan(document, "SL777");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // only the parse; no save
    const error = document.getElementById("putaway-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("SL777");
  });

  it("refuses a non-location scan and keeps the dialog open", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    scan(document, "[)>another bag");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.getElementById("putaway-error").hidden).toBe(false);
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });

  it("falls back to the manual select on submit", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    document.getElementById("putaway-select").value = "9";
    document.getElementById("putaway-form").dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    expect(fetchBody(fetchMock, 1)).toEqual({ location_id: 9 });
    expect(document.querySelector(".ril-location").value).toBe("9");
  });

  it("shows the server's error and keeps the dialog open when the save fails", async () => {
    const { document, fetchMock } = await openOnStagedRow({
      fetchImpl: (url) =>
        url === "/api/shops/parse"
          ? ok({ mpn: "ABC123", distributor_pn: null })
          : Promise.resolve({
              ok: false,
              status: 422,
              json: () => Promise.resolve({ detail: "location not found" }),
            }),
    });

    scan(document, "SL5");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const error = document.getElementById("putaway-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("location not found");
    expect(document.getElementById("putaway-dialog").open).toBe(true);
  });
});

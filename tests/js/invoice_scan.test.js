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
      <input id="invoice-scan-input" />
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
        <input id="putaway-scan" />
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

function pressEnter(document, id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(
    new document.defaultView.KeyboardEvent("keydown", {
      key: "Enter",
      cancelable: true,
      bubbles: true,
    }),
  );
}

describe("invoice_scan.js — bag scan", () => {
  it("parses the code server-side and opens the dialog on the matching staged row", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
    });

    pressEnter(document, "invoice-scan-input", "[)>...1PABC123...");
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/shops/parse");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(fetchMock)).toEqual({ code: "[)>...1PABC123..." });
    const dialog = document.getElementById("putaway-dialog");
    expect(dialog.showModal).toHaveBeenCalledTimes(1);
    expect(document.getElementById("putaway-part").textContent).toBe("ABC123");
    expect(document.getElementById("putaway-desc").textContent).toBe("A widget");
    expect(document.getElementById("invoice-scan-input").value).toBe("");
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

    pressEnter(document, "invoice-scan-input", "bag");
    await tick();
    pressEnter(document, "putaway-scan", "SL9");
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

    pressEnter(document, "invoice-scan-input", "QTY:5 PN:71-ABC123 https://tme.eu/x");
    await tick();

    expect(document.getElementById("putaway-dialog").showModal).toHaveBeenCalled();
    expect(document.getElementById("putaway-part").textContent).toBe("ABC123");
  });

  it("matches a regular line by the distributor part number", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "NOPE-1", distributor_pn: "SPN-9" }),
    });

    pressEnter(document, "invoice-scan-input", "bag");
    await tick();
    pressEnter(document, "putaway-scan", "sl9"); // case-insensitive
    await tick();

    const save = fetchMock.mock.calls[1];
    expect(save[0]).toBe("/api/invoices/7/lines/31/location");
    expect(save[1].method).toBe("PUT");
    expect(fetchBody(fetchMock, 1)).toEqual({ location_id: 9 });
    const row = document.querySelector("#invoice-lines tr");
    expect(row.dataset.locationId).toBe("9");
    expect(row.children[2].textContent).toBe("Lab / Shelf 02");
    expect(document.getElementById("putaway-dialog").close).toHaveBeenCalled();
  });

  it("rejects a location label scanned into the bag field", async () => {
    const { document, fetchMock } = loadPage(scanFixture(), SCRIPTS);
    pressEnter(document, "invoice-scan-input", "SL5");
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
    pressEnter(document, "invoice-scan-input", "bag");
    await tick();

    const status = document.getElementById("invoice-scan-status");
    expect(status.className).toBe("error");
    expect(status.textContent).toContain("UNKNOWN-99");
    expect(document.getElementById("putaway-dialog").showModal).not.toHaveBeenCalled();
  });
});

describe("invoice_scan.js — location scan in the dialog", () => {
  async function openOnStagedRow(overrides = {}) {
    const page = loadPage(scanFixture(), SCRIPTS, {
      fetchImpl: parseRouting({ mpn: "ABC123", distributor_pn: null }),
      ...overrides,
    });
    pressEnter(page.document, "invoice-scan-input", "bag");
    await tick();
    return page;
  }

  it("saves a scanned SL code to the import line and updates the row in place", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    pressEnter(document, "putaway-scan", "SL5");
    await tick();

    const save = fetchMock.mock.calls[1];
    expect(save[0]).toBe("/api/invoices/7/import-lines/21");
    expect(save[1].method).toBe("PATCH");
    expect(fetchBody(fetchMock, 1)).toEqual({ location_id: 5 });
    const row = document.querySelector("#invoice-review tr");
    expect(row.querySelector(".ril-location").value).toBe("5");
    expect(row.classList.contains("is-incomplete")).toBe(false); // type was set
    expect(document.getElementById("putaway-dialog").close).toHaveBeenCalled();
    // A toast names the part and the human-readable path it went to.
    expect(document.querySelector(".toast-ok").textContent).toBe(
      "ABC123 → Lab / Rack A / D1",
    );
  });

  it("refuses a location code that is not on this ShelfOS", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    pressEnter(document, "putaway-scan", "SL777");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1); // only the parse; no save
    const error = document.getElementById("putaway-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("SL777");
  });

  it("refuses a non-location scan and keeps the dialog open", async () => {
    const { document, fetchMock } = await openOnStagedRow();

    pressEnter(document, "putaway-scan", "[)>another bag");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.getElementById("putaway-error").hidden).toBe(false);
    expect(document.getElementById("putaway-dialog").close).not.toHaveBeenCalled();
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

    pressEnter(document, "putaway-scan", "SL5");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const error = document.getElementById("putaway-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("location not found");
    expect(document.getElementById("putaway-dialog").close).not.toHaveBeenCalled();
  });
});

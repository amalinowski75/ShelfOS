import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, bomPickFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "bom_pick.js"];

// The feed shape build_component_table returns: server-driven columns (including
// the selected type's parameter columns) plus rows.
const FEED = {
  columns: [
    { title: "Type", field: "type" },
    { title: "Manufacturer", field: "manufacturer" },
    { title: "MPN", field: "mpn" },
    { title: "Description", field: "notes" },
    { title: "Package", field: "package" },
    { title: "Mounting", field: "mounting_type" },
    { title: "Qty", field: "quantity" },
    { title: "Capacitance", field: "param_7", numeric: true },
  ],
  data: [
    { id: 8, mpn: "GRM188R71H104K", package: "C_0402", quantity: 900 },
    { id: 9, mpn: "CL10B104KB8NNN", package: "C_0603", quantity: 120 },
  ],
};

// A BOM report row, as boms_report.js hands it over.
const LINE = {
  id: 42,
  references: "C1,C2,C3",
  category: "capacitor",
  value: "100n",
  footprint: "C_0402",
  mpn: null,
  quantity: 4,
  total_quantity: 4,
};

function open(page, line = LINE, onDone) {
  return page.window.openBomPicker(line, 7, onDone);
}

// Drive a Tabulator row handler the way the real library would.
function fakeRow(data) {
  return { select: vi.fn(), getData: () => data };
}
const clickEvent = { target: { closest: () => null } };

const feedFetch = (url, opts) => {
  if (String(url).startsWith("/web/api/components")) {
    return Promise.resolve({ ok: true, json: async () => FEED });
  }
  return Promise.resolve({ ok: true, json: async () => ({ id: 1 }), status: 200 });
};

describe("bom_pick.js — opening the picker", () => {
  it("shows the line's designators and facts, and pre-selects its type", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page);
    await tick();

    expect(page.document.getElementById("bom-pick-refs").textContent).toBe("C1,C2,C3");
    const facts = page.document.getElementById("bom-pick-facts").textContent;
    expect(facts).toContain("100n"); // the value it has to match
    expect(facts).toContain("C_0402");
    // "capacitor" names a type we have, so the inventory starts there.
    expect(page.document.getElementById("bom-pick-type").value).toBe("3");
    expect(page.fetchMock.mock.calls[0][0]).toBe("/web/api/components?type_id=3");
  });

  it("falls back to all types when the line's category names none", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page, { ...LINE, category: "thermionic valve" });
    await tick();
    expect(page.document.getElementById("bom-pick-type").value).toBe("");
    expect(page.fetchMock.mock.calls[0][0]).toBe("/web/api/components");
  });

  it("drops the columns that describe the BOM line, keeping what you choose on", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page);
    await tick();

    const fields = page.window.Tabulator.columns.map((c) => c.field);
    // Identity/description columns are the left panel's job, not the picker's.
    for (const hidden of ["type", "manufacturer", "mpn", "notes"]) {
      expect(fields).not.toContain(hidden);
    }
    expect(fields).toEqual(
      expect.arrayContaining(["package", "mounting_type", "quantity", "param_7"]),
    );
    // Details is a link, so it opens in a new tab rather than losing the picker.
    const details = page.window.Tabulator.columns.find((c) => c.field === "_details");
    const html = details.formatter({ getRow: () => ({ getData: () => ({ id: 8 }) }) });
    expect(html).toContain('href="/components/8"');
    expect(html).toContain('target="_blank"');
  });

  it("reloads the inventory when the type is changed", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page);
    await tick();
    const before = page.fetchMock.mock.calls.length;

    const select = page.document.getElementById("bom-pick-type");
    select.value = "";
    select.dispatchEvent(new page.window.Event("change", { bubbles: true }));
    await tick();

    expect(page.fetchMock.mock.calls.length).toBe(before + 1);
    expect(page.fetchMock.mock.calls.at(-1)[0]).toBe("/web/api/components");
  });
});

describe("bom_pick.js — choosing and confirming", () => {
  it("keeps the confirm button disabled until a row is picked", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page);
    await tick();
    const confirm = page.document.getElementById("bom-pick-confirm");
    expect(confirm.disabled).toBe(true);

    page.window.Tabulator.handlers.rowClick(clickEvent, fakeRow(FEED.data[0]));
    expect(confirm.disabled).toBe(false);
    // The MPN column is hidden, so the echo is where the part is actually named.
    expect(page.document.getElementById("bom-pick-selected").textContent).toContain(
      "GRM188R71H104K",
    );
  });

  it("PUTs the assignment for that line, then refreshes the report", async () => {
    const onDone = vi.fn();
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page, LINE, onDone);
    await tick();

    page.window.Tabulator.handlers.rowClick(clickEvent, fakeRow(FEED.data[1]));
    page.document.getElementById("bom-pick-confirm").click();
    await tick();

    const [url, opts] = page.fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/boms/7/lines/42/component");
    expect(opts.method).toBe("PUT");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({ component_id: 9 });
    expect(onDone).toHaveBeenCalled();
  });

  it("a double-click picks and confirms in one go", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page);
    await tick();

    page.window.Tabulator.handlers.rowDblClick(clickEvent, fakeRow(FEED.data[0]));
    await tick();

    const [url, opts] = page.fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/boms/7/lines/42/component");
    expect(JSON.parse(opts.body)).toEqual({ component_id: 8 });
  });

  it("ignores a click that landed on the Details link", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    await open(page);
    await tick();

    const onLink = { target: { closest: (sel) => (sel === "a" ? {} : null) } };
    page.window.Tabulator.handlers.rowClick(onLink, fakeRow(FEED.data[0]));
    expect(page.document.getElementById("bom-pick-confirm").disabled).toBe(true);
  });

  it("surfaces a refusal and lets the user try again", async () => {
    const fetchImpl = (url, opts) => {
      if (String(url).startsWith("/web/api/components")) {
        return Promise.resolve({ ok: true, json: async () => FEED });
      }
      return Promise.resolve({
        ok: false,
        status: 422,
        json: async () => ({ detail: "that component is no longer in use" }),
      });
    };
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl });
    await open(page);
    await tick();

    page.window.Tabulator.handlers.rowClick(clickEvent, fakeRow(FEED.data[0]));
    page.document.getElementById("bom-pick-confirm").click();
    await tick();

    const error = page.document.getElementById("bom-pick-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("no longer in use");
    // Still usable: the button is live again rather than stuck disabled.
    expect(page.document.getElementById("bom-pick-confirm").disabled).toBe(false);
  });
});

describe("bom_pick.js — the page behind it", () => {
  const locked = (page) =>
    page.document.documentElement.classList.contains("bom-pick-open");

  it("locks the page while open so its scrollbar isn't a second one", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    expect(locked(page)).toBe(false);
    await open(page);
    await tick();
    expect(locked(page)).toBe(true);
  });

  it("unlocks on every way out, so the page is never left stuck", async () => {
    const page = loadPage(bomPickFixture(), SCRIPTS, { fetchImpl: feedFetch });
    const dialog = page.document.getElementById("bom-pick-dialog");

    // The dialog's own close event.
    await open(page);
    await tick();
    dialog.dispatchEvent(new page.window.Event("close"));
    expect(locked(page)).toBe(false);

    // Clicking Cancel or the ×. Not covered by the close event alone: a real
    // dialog.close() was seen firing none, which left the page stuck.
    await open(page);
    await tick();
    dialog.querySelector("[data-close]").click();
    expect(locked(page)).toBe(false);

    // Escape.
    await open(page);
    await tick();
    dialog.dispatchEvent(new page.window.Event("cancel"));
    expect(locked(page)).toBe(false);

    // …and so does a successful assignment.
    await open(page);
    await tick();
    page.window.Tabulator.handlers.rowClick(clickEvent, fakeRow(FEED.data[0]));
    page.document.getElementById("bom-pick-confirm").click();
    await tick();
    expect(locked(page)).toBe(false);
  });
});

describe("bom_pick.js — absent for read-only", () => {
  it("does nothing on a page without the picker markup", () => {
    const { window } = loadPage("<div></div>", SCRIPTS);
    expect(window.openBomPicker).toBeUndefined();
  });
});

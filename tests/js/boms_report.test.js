import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, bomReportFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "boms_report.js"];

// The formatters, the summary renderer and loadReport are top-level functions in
// boms_report.js, so the harness exposes them on the page's window. We exercise
// them directly (the real Tabulator library isn't available under jsdom).
function fakeCell(value, rowData = {}) {
  return { getValue: () => value, getRow: () => ({ getData: () => rowData }) };
}

describe("boms_report.js — rendering", () => {
  it("fills the summary banner from the report summary", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    window.renderBomSummary({
      buildable: 3, ok: 2, short: 1, out: 4, unresolved: 6,
    });
    const html = document.getElementById("bom-summary").innerHTML;
    expect(html).toContain("<strong>3</strong>");
    expect(html).toContain("buildable");
    expect(html).toContain("6 unresolved");
    // Only assigned lines feed the buildable figure now; the headline must not
    // claim a kind of match it no longer rests on.
    expect(html).not.toContain("matched and assigned");
  });

  it("shows 0 buildable when the count is null (nothing resolved)", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    window.renderBomSummary({
      buildable: null, ok: 0, short: 0, out: 0, unresolved: 3,
    });
    expect(document.getElementById("bom-summary").innerHTML).toContain(
      "<strong>0</strong>",
    );
  });

  it("maps each status to its badge class and label", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomStatusFormatter(fakeCell("ok"))).toContain("b-ok");
    expect(window.bomStatusFormatter(fakeCell("ok"))).toContain("in stock");
    const unresolved = window.bomStatusFormatter(fakeCell("unresolved"));
    expect(unresolved).toContain("b-neutral");
    expect(unresolved).toContain("unresolved");
  });

  it("shows a real stock figure for a resolved line, MPN or not", () => {
    // The dash means "we are not saying"; an assignment IS the lookup, so its
    // stock is a number — including a genuine 0.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const assigned = { component_id: 8, mpn: "GRM188" };
    const row = { mpn: null, assigned, resolved: true };
    expect(window.bomStockFormatter(fakeCell(900, row))).toBe("900");
    expect(window.bomStockFormatter(fakeCell(0, row))).toBe("0");
    expect(window.bomStockFormatter(fakeCell(12, { mpn: "R-1", resolved: true }))).toBe(
      "12",
    );
  });

  it("refuses to print a stock figure for an unresolved line", () => {
    // The feed's number is the sum over every component sharing the MPN, across
    // manufacturers. Printing it beside an "unresolved" badge is the claim the
    // whole status change exists to stop — and the row is not even clickable.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomStockFormatter(fakeCell(40, { mpn: "MCP2200" }))).toBe("—");
    expect(window.bomStockFormatter(fakeCell(0, { mpn: null }))).toBe("—");
  });

  it("links each substitute (single line) to its component", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomSubstitutesFormatter(
      fakeCell([
        { component_id: 8, mpn: "INI-5747", value: "10 kΩ", stock: 240, exact: true },
        { component_id: 9, mpn: "INI-4700", value: "4.7 kΩ", stock: 610, exact: false },
      ]),
    );
    expect(html).toContain('href="/components/8"');
    expect(html).toContain("10 kΩ");
    expect(html).toContain('href="/components/9"');
    expect(html).toContain(" · "); // dot-separated on one line
  });

  it("puts full substitute detail (footprint, mpn, stock, exact) in the tooltip", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const tip = window.bomSubstitutesTooltip([
      {
        component_id: 8,
        value: "10 kΩ",
        package: "0402",
        mpn: "INI-5747",
        stock: 240,
        exact: true,
      },
    ]);
    // footprint comes right after the value
    expect(tip).toContain("10 kΩ · 0402");
    expect(tip).toContain("INI-5747");
    expect(tip).toContain("stock 240");
    expect(tip).toContain("exact");
  });

  it("shows a dash when a line has no substitutes", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomSubstitutesFormatter(fakeCell([]))).toContain("—");
  });

  // The CSV content is untrusted; a substitute's value derives from uploaded data,
  // so the formatter must HTML-escape it (CSV-XSS regression).
  it("escapes an untrusted substitute value", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomSubstitutesFormatter(
      fakeCell([
        { component_id: 5, value: "<script>alert(1)</script>", stock: 3, exact: false },
      ]),
    );
    expect(html).not.toContain("<script>alert(1)</script>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  // Tabulator renders tooltip content via innerHTML, so tooltip text is an XSS
  // sink too — both tooltips must escape their untrusted fields.
  it("escapes untrusted fields in the substitute tooltip", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const tip = window.bomSubstitutesTooltip([
      {
        component_id: 5,
        value: "<script>alert(1)</script>",
        package: "<b>x</b>",
        mpn: '<img src=x onerror=1>',
        stock: 3,
        exact: false,
      },
    ]);
    expect(tip).not.toContain("<script>");
    expect(tip).not.toContain("<img");
    expect(tip).not.toContain("<b>");
    expect(tip).toContain("&lt;script&gt;");
  });

  it("escapes the references tooltip", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const tip = window.bomReferencesTooltip(null, fakeCell("R1<img src=x onerror=1>"));
    expect(tip).not.toContain("<img");
    expect(tip).toContain("&lt;img");
  });

  it("escapes an untrusted MPN cell", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomMpnFormatter(fakeCell("<img src=x onerror=1>"));
    expect(html).not.toContain("<img src=x onerror=1>");
    expect(html).toContain("&lt;img");
  });
});

describe("boms_report.js — add to inventory", () => {
  it("offers the action only when nothing in inventory answers the line", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomCanAdd({ matched: [] })).toBe(true); // MPN nothing carries
    expect(window.bomCanAdd({})).toBe(true); // no MPN to look up at all
    // An unresolved line with candidates is a choice to make, not a part to
    // create — offering "Add to inventory" here is how duplicates get made.
    expect(window.bomCanAdd({ matched: [{ component_id: 8 }] })).toBe(false);
  });

  it("seeds the prefill from a line, with a numeric value only for passives", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(
      window.bomAddPrefill({
        category: "resistor",
        value: "10k 1%",
        mpn: "R-1",
        manufacturer: "YAGEO",
      }),
    ).toEqual({ category: "resistor", value: "10k 1%", mpn: "R-1", manufacturer: "YAGEO" });
    // A non-passive "value" is a part name, so it's dropped from the prefill.
    expect(
      window.bomAddPrefill({ category: "ic", value: "STM32", mpn: "STM32", manufacturer: "ST" }),
    ).toEqual({ category: "ic", value: null, mpn: "STM32", manufacturer: "ST" });
  });
});

describe("boms_report.js — assigned component", () => {
  it("shows the assigned part, linked, and a dash when there is none", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomAssignedFormatter(fakeCell(null))).toContain("—");

    const html = window.bomAssignedFormatter(
      fakeCell({ component_id: 8, mpn: "GRM188", deleted: false }),
    );
    expect(html).toContain('href="/components/8"');
    expect(html).toContain("GRM188");
    expect(html).not.toContain("not in use");
  });

  it("flags an assignment whose part was taken out of use", () => {
    // Dropping it silently would leave the line looking untouched.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomAssignedFormatter(
      fakeCell({ component_id: 8, mpn: "GRM188", deleted: true }),
    );
    expect(html).toContain("not in use");
  });

  it("escapes an assigned MPN (it can come from an uploaded CSV's part)", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomAssignedFormatter(
      fakeCell({ component_id: 8, mpn: "<img src=x>", deleted: false }),
    );
    expect(html).not.toContain("<img src=x>");
    expect(html).toContain("&lt;img");
  });

  it("offers Assign on every line, and Change/Remove once one is assigned", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    // A line that already matches its MPN can still be built from something else.
    const ok = window.bomActionButtons({
      status: "ok",
      assigned: null,
      matched: [{ component_id: 8 }],
    });
    expect(ok).toContain('data-act="assign-component"');
    expect(ok).toContain("Assign");
    expect(ok).not.toContain("add-component"); // nothing missing to add
    expect(ok).not.toContain("unassign-component");

    const unknown = window.bomActionButtons({ status: "unresolved", matched: [] });
    expect(unknown).toContain('data-act="add-component"');

    const assigned = window.bomActionButtons({
      status: "ok",
      assigned: { component_id: 8, mpn: "X" },
    });
    expect(assigned).toContain("Change");
    expect(assigned).toContain('data-act="unassign-component"');
    // "Add to inventory" would be beside the point once a part is chosen.
    expect(assigned).not.toContain("add-component");
  });

  it("drops Add to inventory on an assigned line even when nothing matched", () => {
    // The one shape that would offer "Add to inventory" AND carry an assignment:
    // a line whose MPN matches nothing, assigned by hand to a part since retired.
    // Asserting it against a row that never offers the button would prove nothing
    // about the assignment.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const stillOffered = window.bomActionButtons({
      status: "unresolved",
      matched: [],
      assigned: null,
    });
    expect(stillOffered).toContain("add-component");

    const html = window.bomActionButtons({
      status: "unresolved",
      matched: [],
      assigned: { component_id: 8, mpn: "X", deleted: true },
    });
    expect(html).not.toContain("add-component"); // the way out is Change / Remove
    expect(html).toContain("Change");
    expect(html).toContain('data-act="unassign-component"');
  });

  it("keeps the actions visible rather than hiding them behind a hover", () => {
    // `.row-actions` is hover-only in app.css; Assign is the point of the row.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomActionButtons({
      status: "ok",
      assigned: null,
      matched: [{ component_id: 8 }],
    });
    expect(html).toContain("bom-row-actions");
    expect(html).not.toContain('class="row-actions"');
  });

  it("DELETEs the assignment and refreshes on Remove", async () => {
    const onDone = vi.fn();
    const { window, fetchMock } = loadPage(bomReportFixture(), SCRIPTS);
    await window.bomUnassign("7", 42, onDone);

    const [url, opts] = fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/boms/7/lines/42/component");
    expect(opts.method).toBe("DELETE");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(onDone).toHaveBeenCalled();
  });

  it("does not refresh when removing was refused", async () => {
    const onDone = vi.fn();
    const fetchImpl = () =>
      Promise.resolve({ ok: false, json: async () => ({ detail: "nope" }) });
    const { window } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    await window.bomUnassign("7", 42, onDone);
    expect(window.alert).toHaveBeenCalledWith("nope");
    expect(onDone).not.toHaveBeenCalled();
  });
});

describe("boms_report.js — ordered", () => {
  const orderedColumn = (window) =>
    window.bomReportColumns().find((c) => c.field === "ordered");

  it("offers the tick as a real checkbox to a writer", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const on = window.bomOrderedFormatter(fakeCell(true, { references: "R1" }));
    expect(on).toContain("checkbox");
    expect(on).toContain("checked");
    const off = window.bomOrderedFormatter(fakeCell(false, { references: "R1" }));
    expect(off).toContain("checkbox");
    expect(off).not.toContain("checked");
    // Named after its line, so a column of identical boxes is distinguishable.
    expect(on).toContain('aria-label="Ordered — R1"');
  });

  it("shows a read-only account the state without a control it cannot use", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS, { role: "read-only" });
    expect(window.bomOrderedFormatter(fakeCell(true, {}))).not.toContain("checkbox");
    expect(window.bomOrderedFormatter(fakeCell(true, {}))).toContain("✓");
    expect(window.bomOrderedFormatter(fakeCell(false, {}))).toContain("—");
  });

  it("escapes the designators it names the checkbox after", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomOrderedFormatter(
      fakeCell(false, { references: '"><img src=x>' }),
    );
    expect(html).not.toContain("<img src=x>");
  });

  it("PUTs the new state for that line", async () => {
    const { window, fetchMock } = loadPage(bomReportFixture(), SCRIPTS);
    const saved = await window.bomSetOrdered("7", 42, true, null);

    const [url, opts] = fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/boms/7/lines/42/ordered");
    expect(opts.method).toBe("PUT");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({ ordered: true });
    expect(saved).toBe(true);
  });

  it("puts the box back when the server refuses", async () => {
    // Otherwise the tick shows a state that was never stored.
    const fetchImpl = () =>
      Promise.resolve({ ok: false, json: async () => ({ detail: "nope" }) });
    const { window } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    const box = window.document.createElement("input");
    box.type = "checkbox";
    box.checked = true; // the user just ticked it

    const saved = await window.bomSetOrdered("7", 42, true, box);
    expect(saved).toBe(false);
    expect(box.checked).toBe(false); // reverted
    expect(window.alert).toHaveBeenCalledWith("nope");
  });

  it("ticking the box does not navigate to the component page", () => {
    // The row click opens the matched component; a checkbox is neither a link nor
    // a button, so without the guard ticking one would leave the report. jsdom
    // doesn't navigate, so assert on the lookup the handler makes on its way there
    // — that IS the branch, and it's observable.
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    const row = { getData: () => ({ matched: [{ component_id: 8 }] }) };
    const target = window.bomRowTarget;
    const spy = vi.fn(target);
    window.bomRowTarget = spy;
    try {
      const input = document.createElement("input");
      window.Tabulator.handlers.rowClick({ target: input }, row);
      expect(spy).not.toHaveBeenCalled(); // guarded: the click was the checkbox's

      window.Tabulator.handlers.rowClick({ target: document.createElement("td") }, row);
      expect(spy).toHaveBeenCalled(); // …and an ordinary cell still navigates
    } finally {
      window.bomRowTarget = target;
    }
  });

  it("does not navigate when the click lands beside the box, inside its cell", () => {
    // Guarding the checkbox alone leaves the rest of the cell live: a near-miss
    // would open the component page, which is the annoyance this column removes.
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    const row = { getData: () => ({ matched: [{ component_id: 8 }] }) };
    const target = window.bomRowTarget;
    const spy = vi.fn(target);
    window.bomRowTarget = spy;
    try {
      const cell = document.createElement("div");
      cell.className = "tabulator-cell bom-ordered-cell"; // the cell, not the input
      window.Tabulator.handlers.rowClick({ target: cell }, row);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      window.bomRowTarget = target;
    }
  });

  it("keeps the row's data in step after a tick, so a redraw can't undo it", async () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    const update = vi.fn();
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.act = "ordered";
    box.checked = true; // the browser flipped it before the handler ran
    const cell = { getRow: () => ({ getData: () => ({ id: 42 }), update }) };

    orderedColumn(window).cellClick({ target: box }, cell);
    await tick();
    expect(update).toHaveBeenCalledWith({ ordered: true });
  });

  it("drops a second click while the first is still in flight", async () => {
    // Two PUTs would race: what is stored is the last to land, what is shown is the
    // last to resolve, and those need not agree.
    let release;
    const held = new Promise((resolve) => (release = resolve));
    const fetchImpl = () => held.then(() => ({ ok: true, json: async () => ({}) }));
    const { window, document, fetchMock } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl,
    });
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;

    const first = window.bomSetOrdered("7", 42, true, box);
    const second = await window.bomSetOrdered("7", 42, false, box);
    expect(second).toBe(false);
    expect(fetchMock.mock.calls.length).toBe(1); // the second click sent nothing

    release();
    expect(await first).toBe(true);
    // …and the lock is released, so the box still works afterwards.
    await window.bomSetOrdered("7", 42, false, box);
    expect(fetchMock.mock.calls.length).toBe(2);
  });

  it("filters the column by ticked / unticked", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const column = orderedColumn(window);
    expect(column.headerFilter).toBe("tickCross");
    // Tristate: the third state means "don't filter", not "unticked".
    expect(column.headerFilterEmptyCheck(null)).toBe(true);
    expect(column.headerFilterEmptyCheck(false)).toBe(false);
  });
});

describe("boms_report.js — row navigation", () => {
  it("targets a RESOLVED line's component, and nothing else", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(
      window.bomRowTarget({
        resolved: true,
        matched: [{ component_id: 8 }, { component_id: 9 }],
      }),
    ).toBe("/components/8");
    // An unresolved line's candidates are exactly what the report refuses to
    // claim is this line's part; sending someone there is the guess we removed.
    expect(
      window.bomRowTarget({ resolved: false, matched: [{ component_id: 8 }] }),
    ).toBe(null);
    expect(window.bomRowTarget({ resolved: true, matched: [] })).toBe(null);
    expect(window.bomRowTarget({})).toBe(null);
  });
});

describe("boms_report.js — loadReport", () => {
  it("fills the summary and sets the rows on success", async () => {
    const report = {
      summary: { buildable: 2, ok: 1, short: 0, out: 0, unresolved: 0 },
      lines: [{ references: "R1" }],
    };
    const fetchImpl = () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(report) });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.fn(() => Promise.resolve());
    await window.loadReport({ setData }, "7");
    expect(document.getElementById("bom-summary").innerHTML).toContain(
      "<strong>2</strong>",
    );
    expect(setData).toHaveBeenCalledWith(report.lines);
  });

  it("shows an error and clears the table when the feed fails", async () => {
    const fetchImpl = () => Promise.resolve({ ok: false, json: async () => ({}) });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.fn(() => Promise.resolve());
    await window.loadReport({ setData }, "7");
    expect(document.getElementById("bom-summary").innerHTML).toContain(
      "Could not load",
    );
    expect(setData).toHaveBeenCalledWith([]);
  });

  it("puts the scroll back where it was instead of jumping to the top", async () => {
    // Every per-line action reloads this table; setData scrolls it home, which on a
    // long BOM means hunting for the line you were just on, every time.
    const report = {
      summary: { buildable: 1, boards: 1 },
      lines: [{ references: "R1" }],
    };
    const fetchImpl = () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(report) });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });

    // A table whose holder is scrolled, as Tabulator lays one out.
    const element = document.createElement("div");
    const holder = document.createElement("div");
    holder.className = "tabulator-tableholder";
    element.appendChild(holder);
    holder.scrollTop = 640;
    const table = {
      element,
      setData: () => {
        holder.scrollTop = 0; // what setData does
        return Promise.resolve();
      },
      setHeight: () => {},
    };

    await window.loadReport(table, "7");
    expect(holder.scrollTop).toBe(640);
  });

  it("restores it again once the rows exist, not just once", async () => {
    // The immediate set is not enough on its own: until Tabulator has rendered the
    // rows the holder is too short to hold the offset, and the browser silently
    // clamps it to 0. This holder behaves that way, so it reaches the retry — the
    // half written for a case the simpler fake can never produce.
    const report = { summary: { buildable: 1, boards: 1 }, lines: [{ references: "R1" }] };
    const fetchImpl = () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(report) });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });

    const element = document.createElement("div");
    const holder = document.createElement("div");
    holder.className = "tabulator-tableholder";
    element.appendChild(holder);

    let rendered = true;
    let value = 0;
    Object.defineProperty(holder, "scrollTop", {
      get: () => value,
      set: (v) => {
        value = rendered ? v : 0; // short container → clamped to the top
      },
    });
    holder.scrollTop = 640;

    const table = {
      element,
      setData: () => {
        rendered = false; // rows are gone until the render lands
        holder.scrollTop = 0;
        setTimeout(() => {
          rendered = true;
        }, 0);
        return Promise.resolve();
      },
      setHeight: () => {},
    };

    await window.loadReport(table, "7");
    expect(holder.scrollTop).toBe(0); // the immediate set was clamped away
    await tick(); // …and the retry, once the rows are there, gets it back
    expect(holder.scrollTop).toBe(640);
  });

  it("shows an error when the request throws", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.fn(() => Promise.resolve());
    await window.loadReport({ setData }, "7");
    expect(document.getElementById("bom-summary").innerHTML).toContain(
      "Could not load",
    );
  });
});

describe("boms_report.js — building several boards", () => {
  const okReport = {
    summary: { buildable: 2, ok: 1, short: 0, out: 0, unresolved: 0, boards: 1 },
    lines: [{ references: "R1" }],
  };
  const okFetch = () =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(okReport) });

  it("asks the feed for the board count in the box", async () => {
    const { window, document, fetchMock } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: okFetch,
    });
    document.getElementById("bom-boards").value = "10";
    await window.loadReport({ setData: vi.fn(() => Promise.resolve()) }, "7");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/boms/7/report?boards=10");
  });

  it("falls back to one board for a blank or nonsensical count", async () => {
    const { window, document, fetchMock } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: okFetch,
    });
    const input = document.getElementById("bom-boards");
    for (const bad of ["", "0", "-3"]) {
      input.value = bad;
      await window.loadReport({ setData: vi.fn(() => Promise.resolve()) }, "7");
      expect(fetchMock.mock.calls.at(-1)[0]).toBe("/api/boms/7/report?boards=1");
    }
  });

  it("remembers the count per BOM across visits", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    window.bomBoardsRemember("7", 12);
    expect(window.bomBoardsStored("7")).toBe(12);
    expect(window.bomBoardsStored("9")).toBe(1); // a different BOM is unaffected
  });

  it("stores the count when the box changes, and restores it on the next visit", () => {
    // Pins the wiring, not just the pair of helpers: typing a count has to persist
    // it, and opening the page again has to put it back in the box.
    const page = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl: okFetch });
    const input = page.document.getElementById("bom-boards");
    input.value = "25";
    input.dispatchEvent(new page.window.Event("change", { bubbles: true }));
    expect(page.window.bomBoardsStored("7")).toBe(25);

    // A fresh page (same storage) opens with the remembered count in the box.
    const again = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: okFetch,
      localStorage: { "shelfos:bom-boards:7": "25" },
    });
    expect(again.document.getElementById("bom-boards").value).toBe("25");
  });

  it("says how many of the requested boards are buildable", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    window.renderBomSummary({ buildable: 3, ok: 1, short: 1, boards: 10 });
    const html = document.getElementById("bom-summary").innerHTML;
    expect(html).toContain("<strong>3</strong>");
    expect(html).toContain("of 10 requested");

    // A single board doesn't need the qualifier.
    window.renderBomSummary({ buildable: 3, ok: 1, boards: 1 });
    expect(document.getElementById("bom-summary").innerHTML).not.toContain(
      "requested",
    );
  });

  it("tells a short line how many boards its stock covers", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const short = window.bomStatusFormatter(
      fakeCell("short", { mpn: "RES-1K", boards_possible: 5 }),
    );
    expect(short).toContain("short");
    expect(short).toContain("enough for 5");

    // The other statuses need no such note: "ok" covers the run, "out" is zero by
    // definition, and an unresolved line has no stock figure to speak of.
    for (const status of ["ok", "out", "unresolved"]) {
      expect(
        window.bomStatusFormatter(fakeCell(status, { mpn: "RES-1K", boards_possible: 0 })),
      ).not.toContain("enough for");
    }
  });

  it("drops the note when it would only say zero", () => {
    // At one board — the default view — short means the stock doesn't cover even
    // one, so the count is always 0: it repeats the badge and reads like a stock
    // figure, cutting against the very thing "short" is there to say.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(
      window.bomStatusFormatter(fakeCell("short", { mpn: "RES-1K", boards_possible: 0 })),
    ).not.toContain("enough for");
  });

  it("says how many the run is short by, and shows a dash when it can't", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomMissingFormatter(fakeCell(10))).toContain("<strong>10</strong>");
    // Covered is a fact worth printing, unlike "we don't know".
    expect(window.bomMissingFormatter(fakeCell(0))).toContain(">0<");
    expect(window.bomMissingFormatter(fakeCell(null))).toContain("—");
  });

  it("puts the shortfall right after the stock figure", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const fields = window.bomReportColumns().map((c) => c.field);
    expect(fields.indexOf("missing")).toBe(fields.indexOf("stock") + 1);
  });

  it("offers both the per-board and the run total as columns", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const fields = window.bomReportColumns().map((c) => c.field);
    expect(fields).toContain("quantity");
    expect(fields).toContain("total_quantity");
  });
});

describe("boms_report.js — reload from CSV", () => {
  it("re-parses the stored CSV, then re-reads the report", async () => {
    const calls = [];
    const fetchImpl = (url, opts) => {
      calls.push([url, opts?.method || "GET"]);
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({ summary: { buildable: 1, boards: 1 }, lines: [] }),
      });
    };
    const { document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("bom-reload").click();
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(calls[0]).toEqual(["/api/boms/7/reimport", "POST"]);
    expect(calls[1][0]).toContain("/api/boms/7/report");
    expect(document.getElementById("bom-status").hidden).toBe(false);
    expect(document.getElementById("bom-status").textContent).toContain(
      "rebuilt",
    );
  });

  it("reports a refusal without touching the table", async () => {
    const fetchImpl = (url, opts) =>
      opts?.method === "POST"
        ? Promise.resolve({
            ok: false,
            status: 422,
            json: async () => ({ detail: "the original CSV is no longer stored" }),
          })
        : Promise.resolve({ ok: true, json: async () => ({ summary: {}, lines: [] }) });
    const { document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("bom-reload").click();
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    const status = document.getElementById("bom-status");
    expect(status.hidden).toBe(false);
    expect(status.className).toBe("error");
    expect(status.textContent).toContain("no longer stored");
  });
});

describe("boms_report.js — resolving the unresolved lines", () => {
  const summaryWith = (unresolved) => ({
    buildable: 0, ok: 1, short: 0, out: 0, unresolved, boards: 1,
  });

  it("offers the two remedies only while something is unresolved", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    window.renderBomSummary(summaryWith(4));
    const html = () => document.getElementById("bom-summary").innerHTML;
    expect(html()).toContain('data-act="assign-obvious"');
    expect(html()).toContain('data-act="show-unresolved"');

    // A BOM with nothing left to assign shows no buttons at all — the remedy
    // disappears with the problem rather than sitting there doing nothing.
    window.renderBomSummary(summaryWith(0));
    expect(html()).not.toContain('data-act="assign-obvious"');
    expect(html()).not.toContain('data-act="show-unresolved"');
  });

  it("hides the assign button from a read-only account", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, {
      role: "read-only",
    });
    window.renderBomSummary(summaryWith(4));
    const html = document.getElementById("bom-summary").innerHTML;
    expect(html).not.toContain('data-act="assign-obvious"');
    // Looking at what is unresolved is not a write, so that one stays.
    expect(html).toContain('data-act="show-unresolved"');
  });

  it("posts, reloads the report, then says what it settled", async () => {
    const calls = [];
    const fetchImpl = (url, opts) => {
      calls.push([url, opts?.method || "GET", opts?.headers]);
      return url.endsWith("/assign-obvious")
        ? Promise.resolve({ ok: true, json: async () => ({ assigned: 3 }) })
        : Promise.resolve({
            ok: true,
            json: async () => ({ summary: summaryWith(1), lines: [] }),
          });
    };
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    window.renderBomSummary(summaryWith(4));
    document.querySelector('[data-act="assign-obvious"]').click();
    await tick();
    await tick();

    expect(calls[0][0]).toBe("/api/boms/7/assign-obvious");
    expect(calls[0][1]).toBe("POST");
    expect(calls[0][2]["X-CSRF-Token"]).toBe(CSRF);
    // The report is re-read before anything is said: what is LEFT is a number
    // only the fresh report knows.
    expect(calls[1][0]).toContain("/api/boms/7/report");
    const status = document.getElementById("bom-status");
    expect(status.hidden).toBe(false);
    expect(status.textContent).toContain("Assigned 3");
  });

  it("says so plainly when nothing could be settled", async () => {
    const fetchImpl = (url) =>
      url.endsWith("/assign-obvious")
        ? Promise.resolve({ ok: true, json: async () => ({ assigned: 0 }) })
        : Promise.resolve({
            ok: true,
            json: async () => ({ summary: summaryWith(4), lines: [] }),
          });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    window.renderBomSummary(summaryWith(4));
    document.querySelector('[data-act="assign-obvious"]').click();
    await tick();
    await tick();

    expect(document.getElementById("bom-status").textContent).toContain(
      "Nothing could be assigned",
    );
  });

  it("reports a refusal and leaves the button usable", async () => {
    const fetchImpl = () =>
      Promise.resolve({
        ok: false,
        status: 403,
        json: async () => ({ detail: "read-only accounts cannot write" }),
      });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    window.renderBomSummary(summaryWith(4));
    const button = document.querySelector('[data-act="assign-obvious"]');
    button.click();
    await tick();
    await tick();

    const status = document.getElementById("bom-status");
    expect(status.className).toBe("error");
    expect(status.textContent).toContain("read-only accounts cannot write");
    // Nothing was reloaded, so this button is still the one on the page: leaving
    // it disabled would strand the user on a failure they could just retry.
    expect(button.disabled).toBe(false);
  });

  it("filters the table through the Status column's own header filter", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    window.renderBomSummary(summaryWith(4));
    document.querySelector('[data-act="show-unresolved"]').click();

    expect(window.Tabulator.headerFilterSet).toEqual([
      { field: "status", value: "unresolved" },
    ]);
  });
});

describe("boms_report.js — when the reload after a write fails", () => {
  const summaryWith = (unresolved) => ({
    buildable: 0, ok: 1, short: 0, out: 0, unresolved, boards: 1,
  });

  it("says the assignments were stored even though the report is not there", async () => {
    // Silence, or a plain "Assigned 3" under a "Could not load the report"
    // summary, both invite the user to run a write that already happened.
    const fetchImpl = (url) =>
      url.endsWith("/assign-obvious")
        ? Promise.resolve({ ok: true, json: async () => ({ assigned: 3 }) })
        : Promise.resolve({ ok: false, status: 500, json: async () => ({}) });
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS, { fetchImpl });
    window.renderBomSummary(summaryWith(4));
    document.querySelector('[data-act="assign-obvious"]').click();
    await tick();
    await tick();

    const status = document.getElementById("bom-status");
    expect(status.className).toBe("error");
    expect(status.textContent).toContain("Assigned 3");
    expect(status.textContent).toContain("could not be re-read");
  });

  it("tells loadReport's callers whether the report actually arrived", async () => {
    const ok = { summary: summaryWith(0), lines: [] };
    const { window } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ok }),
    });
    const table = { setData: vi.fn(() => Promise.resolve()) };
    expect(await window.loadReport(table, "7")).toBe(true);

    const { window: w2 } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: false, json: async () => ({}) }),
    });
    expect(await w2.loadReport({ setData: vi.fn(() => Promise.resolve()) }, "7")).toBe(
      false,
    );
  });

  it("clears its own unresolved filter once nothing is unresolved", async () => {
    // Otherwise the table shows "No lines" under a summary reporting stock, with
    // the button that set the filter no longer on the page to explain it.
    const report = { summary: summaryWith(0), lines: [{ references: "R1" }] };
    const { window } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => report }),
    });
    window.Tabulator.headerFilterValues.status = "unresolved";
    const table = window.Tabulator.instances[0];
    table.setData = vi.fn(() => Promise.resolve());
    await window.loadReport(table, "7");
    expect(window.Tabulator.headerFilterValues.status).toBe("");
  });

  it("leaves a filter alone while lines are still unresolved", async () => {
    const report = { summary: summaryWith(2), lines: [{ references: "R1" }] };
    const { window } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => report }),
    });
    window.Tabulator.headerFilterValues.status = "unresolved";
    const table = window.Tabulator.instances[0];
    table.setData = vi.fn(() => Promise.resolve());
    await window.loadReport(table, "7");
    expect(window.Tabulator.headerFilterValues.status).toBe("unresolved");
  });

  it("does not touch a filter the user set to something else", async () => {
    const report = { summary: summaryWith(0), lines: [] };
    const { window } = loadPage(bomReportFixture(), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => report }),
    });
    window.Tabulator.headerFilterValues.status = "short";
    const table = window.Tabulator.instances[0];
    table.setData = vi.fn(() => Promise.resolve());
    await window.loadReport(table, "7");
    expect(window.Tabulator.headerFilterValues.status).toBe("short");
  });
});

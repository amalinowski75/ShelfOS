import { describe, it, expect, vi } from "vitest";
import { loadPage, CSRF, bomReportFixture } from "./harness.js";

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
      buildable: 3, ok: 2, short: 1, out: 4, missing: 5, no_mpn: 6,
    });
    const html = document.getElementById("bom-summary").innerHTML;
    expect(html).toContain("<strong>3</strong>");
    expect(html).toContain("buildable");
    expect(html).toContain("without");
    // An assigned line feeds this figure too, and may carry no MPN at all, so the
    // headline must not still claim the count comes from MPN matches.
    expect(html).not.toContain("exact MPN matches");
    expect(html).toContain("matched and assigned");
  });

  it("shows 0 buildable when the count is null (no exact matches)", () => {
    const { window, document } = loadPage(bomReportFixture(), SCRIPTS);
    window.renderBomSummary({
      buildable: null, ok: 0, short: 0, out: 0, missing: 0, no_mpn: 3,
    });
    expect(document.getElementById("bom-summary").innerHTML).toContain(
      "<strong>0</strong>",
    );
  });

  it("maps each status to its badge class and label", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomStatusFormatter(fakeCell("ok"))).toContain("b-ok");
    expect(window.bomStatusFormatter(fakeCell("ok"))).toContain("in stock");
    expect(window.bomStatusFormatter(fakeCell("missing"))).toContain(
      "not in inventory",
    );
    expect(window.bomStatusFormatter(fakeCell("no_mpn"))).toContain("b-neutral");
  });

  it("renders a stock dash for a line without an MPN", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomStockFormatter(fakeCell(0, { mpn: null }))).toBe("—");
    expect(window.bomStockFormatter(fakeCell(12, { mpn: "R-1" }))).toBe("12");
  });

  it("shows a real stock figure for an assigned line, MPN or not", () => {
    // The dash means "nothing was looked up"; an assignment IS the lookup, so its
    // stock is a number — including a genuine 0.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const assigned = { component_id: 8, mpn: "GRM188" };
    expect(window.bomStockFormatter(fakeCell(900, { mpn: null, assigned }))).toBe("900");
    expect(window.bomStockFormatter(fakeCell(0, { mpn: null, assigned }))).toBe("0");
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
  it("offers the action only on unmatched lines", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(window.bomCanAdd("missing")).toBe(true);
    expect(window.bomCanAdd("no_mpn")).toBe(true);
    expect(window.bomCanAdd("ok")).toBe(false);
    expect(window.bomCanAdd("short")).toBe(false);
    expect(window.bomCanAdd("out")).toBe(false);
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
    const ok = window.bomActionButtons({ status: "ok", assigned: null });
    expect(ok).toContain('data-act="assign-component"');
    expect(ok).toContain("Assign");
    expect(ok).not.toContain("add-component"); // nothing missing to add
    expect(ok).not.toContain("unassign-component");

    const missing = window.bomActionButtons({ status: "missing", assigned: null });
    expect(missing).toContain('data-act="add-component"');

    const assigned = window.bomActionButtons({
      status: "ok",
      assigned: { component_id: 8, mpn: "X" },
    });
    expect(assigned).toContain("Change");
    expect(assigned).toContain('data-act="unassign-component"');
    // "Add to inventory" would be beside the point once a part is chosen.
    expect(assigned).not.toContain("add-component");
  });

  it("drops Add to inventory on an assigned line even when its status invites it", () => {
    // The status that offers "Add to inventory" AND an assignment at once: an
    // assigned part retired since, which reports `missing`. Asserting it against a
    // status that never offers the button would prove nothing about the assignment.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const stillOffered = window.bomActionButtons({ status: "missing", assigned: null });
    expect(stillOffered).toContain("add-component");

    const html = window.bomActionButtons({
      status: "missing",
      assigned: { component_id: 8, mpn: "X", deleted: true },
    });
    expect(html).not.toContain("add-component"); // the way out is Change / Remove
    expect(html).toContain("Change");
    expect(html).toContain('data-act="unassign-component"');
  });

  it("keeps the actions visible rather than hiding them behind a hover", () => {
    // `.row-actions` is hover-only in app.css; Assign is the point of the row.
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomActionButtons({ status: "ok", assigned: null });
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

describe("boms_report.js — row navigation", () => {
  it("targets the first matched component's detail page, else null", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    expect(
      window.bomRowTarget({ matched: [{ component_id: 8 }, { component_id: 9 }] }),
    ).toBe("/components/8");
    expect(window.bomRowTarget({ matched: [] })).toBe(null); // no match → not clickable
    expect(window.bomRowTarget({})).toBe(null);
  });
});

describe("boms_report.js — loadReport", () => {
  it("fills the summary and sets the rows on success", async () => {
    const report = {
      summary: { buildable: 2, ok: 1, short: 0, out: 0, missing: 0, no_mpn: 0 },
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
    summary: { buildable: 2, ok: 1, short: 0, out: 0, missing: 0, no_mpn: 0, boards: 1 },
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

    // The other statuses need no such note: "ok" covers the run, out/missing are
    // zero by definition, and a line with no MPN was never matched.
    for (const status of ["ok", "out", "missing", "no_mpn"]) {
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
    expect(document.getElementById("bom-reload-status").hidden).toBe(false);
    expect(document.getElementById("bom-reload-status").textContent).toContain(
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

    const status = document.getElementById("bom-reload-status");
    expect(status.hidden).toBe(false);
    expect(status.className).toBe("error");
    expect(status.textContent).toContain("no longer stored");
  });
});

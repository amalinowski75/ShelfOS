import { describe, it, expect, vi } from "vitest";
import { loadPage, bomReportFixture } from "./harness.js";

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

  it("escapes an untrusted MPN cell", () => {
    const { window } = loadPage(bomReportFixture(), SCRIPTS);
    const html = window.bomMpnFormatter(fakeCell("<img src=x onerror=1>"));
    expect(html).not.toContain("<img src=x onerror=1>");
    expect(html).toContain("&lt;img");
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

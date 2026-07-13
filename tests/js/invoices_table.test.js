import { describe, it, expect, vi } from "vitest";
import { loadPage, invoicesPageFixture, tick } from "./harness.js";

const SCRIPTS = ["shared.js", "invoices_table.js"];

// A fake Tabulator cell over a row's data.
const cell = (value, row = {}) => ({
  getValue: () => value,
  getRow: () => ({ getData: () => row }),
});

describe("invoices_table columns", () => {
  it("gives every column a live text header filter, labelled per column", () => {
    const { window } = loadPage(invoicesPageFixture(), SCRIPTS);
    const columns = window.invoiceColumns();
    expect(columns.map((c) => c.field)).toEqual([
      "invoice_number",
      "supplier",
      "invoice_date",
      "net",
      "gross",
      "status",
    ]);
    for (const column of columns) {
      expect(column.headerFilter).toBe("input");
      expect(column.headerFilterPlaceholder).toBe(`Filter ${column.title}…`);
      expect(column.headerFilterParams.elementAttributes["aria-label"]).toBe(
        `Filter ${column.title}`,
      );
    }
  });

  it("links the invoice number to its detail page and escapes it", () => {
    const { window } = loadPage(invoicesPageFixture(), SCRIPTS);
    const invoice = window.invoiceColumns()[0];
    expect(invoice.formatter(cell("<b>INV-1", { id: 7 }))).toBe(
      '<a class="cell-mono" href="/invoices/7">&lt;b&gt;INV-1</a>',
    );
  });

  it("renders the status as a badge", () => {
    const { window } = loadPage(invoicesPageFixture(), SCRIPTS);
    const status = window.invoiceColumns()[5];
    expect(status.formatter(cell("finalized"))).toContain("b-ok");
    expect(status.formatter(cell("draft"))).toContain("b-neutral");
  });

  it("sorts money columns numerically, blanks below any amount", () => {
    const { window } = loadPage(invoicesPageFixture(), SCRIPTS);
    const { sorter } = window.invoiceColumns()[3]; // Net
    expect(sorter("250.50 EUR", "1000.00 EUR")).toBeLessThan(0);
    expect(sorter("2000.00 EUR", "1000.00 EUR")).toBeGreaterThan(0);
    // "—" (a draft's gross) is treated as less than any real amount.
    expect(sorter("—", "0.00 EUR")).toBeLessThan(0);
    // Two blank grosses compare equal (0), never NaN — several drafts at once
    // is common, and a NaN comparator leaves Array.sort's order undefined.
    expect(sorter("—", "—")).toBe(0);
  });
});

describe("invoices_table loading", () => {
  it("fetches the feed and sets the data", async () => {
    const feed = {
      data: [{ id: 1, invoice_number: "INV-1" }],
      truncated: false,
      limit: 200,
    };
    const fetchImpl = () => Promise.resolve({ ok: true, json: async () => feed });
    const { window, document } = loadPage(invoicesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    const table = { setData: vi.fn(() => Promise.resolve()) };
    await window.loadInvoices(table, document.getElementById("invoice-list-hint"));
    await tick();
    expect(table.setData).toHaveBeenCalledWith(feed.data);
    expect(document.getElementById("invoice-list-hint").hidden).toBe(true);
  });

  it("shows the truncation hint only when the feed says the list was capped", async () => {
    const feed = { data: [], truncated: true, limit: 5 };
    const fetchImpl = () => Promise.resolve({ ok: true, json: async () => feed });
    const { window, document } = loadPage(invoicesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    const hint = document.getElementById("invoice-list-hint");
    await window.loadInvoices({ setData: () => Promise.resolve() }, hint);
    await tick();
    expect(hint.hidden).toBe(false);
    expect(hint.textContent).toBe("Showing the 5 most recent invoices.");
  });

  it("clears the loading state and shows an error when the feed fails", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { window, document } = loadPage(invoicesPageFixture(), SCRIPTS, {
      fetchImpl,
    });
    const table = { setData: vi.fn(() => Promise.resolve()) };
    const hint = document.getElementById("invoice-list-hint");
    await window.loadInvoices(table, hint);
    await tick();
    // The table is emptied (drops Tabulator's spinner) and the user is told.
    expect(table.setData).toHaveBeenCalledWith([]);
    expect(hint.hidden).toBe(false);
    expect(hint.className).toBe("error");
    expect(hint.textContent).toContain("Could not load");
  });
});

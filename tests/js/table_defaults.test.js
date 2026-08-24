import { describe, it, expect } from "vitest";
import {
  loadPage,
  invoicesPageFixture,
  usersPageFixture,
  typesAdminPageFixture,
  matchRulesPageFixture,
  bomReportFixture,
  bomPickFixture,
  typePageFixture,
  tick,
} from "./harness.js";

// Every table has to be built with shared.js's TABLE_DEFAULTS. This is not a style
// rule: left on its virtual DOM the library recomputes its padding from the current
// scroll position and clamps it at zero once you are far enough down, from which
// point the content height grows as you drag — and the browser draws the scrollbar
// thumb from scrollTop/scrollHeight, so the thumb slides out from under the pointer.
// A table that silently loses the defaults gets that back, and nothing else notices.
//
// The audit page is deliberately included even though it is the one table with no
// frameTable: it grows 200 rows at a time, so it is the likeliest to need this.
const AUDIT_FIXTURE = `
  <button type="button" id="audit-clear" hidden>Clear</button>
  <div id="audit-table" data-kinds='[]' data-actors='[]'></div>
  <button type="button" id="audit-more" hidden>Show more</button>
  <p id="audit-count"></p>`;

// The audit page loads itself on init, so it needs a feed shaped like the real one.
const auditFeed = () => ({
  ok: true,
  status: 200,
  json: () => Promise.resolve({ data: [], more: false, cursor: null }),
});

const PAGES = [
  ["components", typePageFixture(), ["shared.js", "type_dialog.js", "app.js"], {}],
  ["BOM lines", bomReportFixture(), ["shared.js", "boms_report.js"], {}],
  [
    "audit log",
    AUDIT_FIXTURE,
    ["shared.js", "audit.js"],
    { fetchImpl: () => Promise.resolve(auditFeed()) },
  ],
  ["invoices", invoicesPageFixture(), ["shared.js", "invoices_table.js"], {}],
  ["users", usersPageFixture(), ["shared.js", "users.js"], {}],
  ["types", typesAdminPageFixture(), ["shared.js", "types_admin.js"], {}],
  ["match rules", matchRulesPageFixture(), ["shared.js", "match_rules.js"], {}],
];

describe("table defaults", () => {
  it.each(PAGES)("the %s table is built with them", (_name, fixture, scripts, opts) => {
    const { window } = loadPage(fixture, scripts, opts);
    const defaults = window.eval("TABLE_DEFAULTS");
    // Guard the guard: an empty TABLE_DEFAULTS would make the match below
    // vacuously true, so state what the table is actually owed.
    expect(defaults.renderVertical).toBe("basic");
    expect(defaults.rowHeight).toBeGreaterThan(0);
    expect(window.Tabulator.options).toMatchObject(defaults);
  });

  // The picker's table is built lazily, the first time the dialog is opened, so
  // it needs opening rather than just loading.
  it("the component picker's table is built with them", async () => {
    const feed = {
      columns: [{ title: "MPN", field: "mpn" }],
      data: [{ id: 8, mpn: "GRM188R71H104K" }],
    };
    const { window } = loadPage(bomPickFixture(), ["shared.js", "bom_pick.js"], {
      fetchImpl: () => Promise.resolve({ ok: true, json: () => Promise.resolve(feed) }),
    });
    window.openBomPicker({ id: 42, references: "C1", quantity: 1 }, 7, () => {});
    await tick();

    const defaults = window.eval("TABLE_DEFAULTS");
    expect(defaults.renderVertical).toBe("basic");
    expect(window.Tabulator.options).toMatchObject(defaults);
  });

  it("rowHeight leaves room for the tallest thing a row can hold", () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    const css = window.eval("TABLE_DEFAULTS.rowHeight");
    // A cell is 11px of padding above and below its content, the row adds a 1px
    // border, and app.css pins every in-table .btn — the tallest cell content
    // there is — to 28px. Anything less than that sum clips the buttons.
    expect(css).toBeGreaterThanOrEqual(11 + 28 + 11 + 1);
  });
});

import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, typePageFixture } from "./harness.js";

// app.js drives the component table, the stock dialog and the New Type builder;
// location_tree.js enhances the stock dialog's location picker.
const SCRIPTS = [
  "shared.js",
  "location_tree.js",
  "type_dialog.js",
  "stock_dialog.js",
  "app.js",
];

function fire(el, type) {
  el.dispatchEvent(
    new el.ownerDocument.defaultView.Event(type, { bubbles: true, cancelable: true }),
  );
}

// app.js declares its table helpers as top-level functions, so they land on the
// jsdom window (as `window.openStockDialog` already does in stock.test.js).

describe("app.js — table formatting", () => {
  it("formats each known column and escapes user text", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    const cell = (v) => ({ getValue: () => v });

    const mpn = window.columnDef({ field: "mpn", title: "MPN" });
    expect(mpn.formatter(cell("<b>R"))).toBe(
      '<span class="cell-mpn">&lt;b&gt;R</span>', // esc() defends against injection
    );

    const pkg = window.columnDef({ field: "package", title: "Package" });
    expect(pkg.formatter(cell("0805"))).toBe('<span class="cell-mono">0805</span>');

    const mt = window.columnDef({ field: "mounting_type", title: "MT" });
    expect(mt.formatter(cell("THT"))).toContain("b-accent");
    expect(mt.formatter(cell("SMT"))).toContain("b-neutral");

    const desc = window.columnDef({ field: "notes", title: "Description" });
    // Escaped in BOTH places — the title attribute is as injectable as the body,
    // and a quote in a description would otherwise break out of it.
    expect(desc.formatter(cell('10k 1% "tight"'))).toBe(
      '<span class="cell-desc" title="10k 1% &quot;tight&quot;">' +
        "10k 1% &quot;tight&quot;</span>",
    );
    // A starting width, NOT a maximum: a maxWidth also caps the drag handle,
    // which would leave a long description permanently unreadable in the table.
    expect(desc.width).toBe(260);
    expect(desc.maxWidth).toBeUndefined();

    const qty = window.columnDef({ field: "quantity", title: "Qty" });
    expect(qty.formatter(cell(0))).toContain("is-zero");
    expect(qty.formatter(cell(5))).toBe('<span class="cell-qty">5</span>');

    // Anything else renders as plain text (no formatter).
    expect(window.columnDef({ field: "type", title: "Type" }).formatter).toBeUndefined();
  });

  it("gives every data column a live text header filter, but not the actions column", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    for (const field of ["mpn", "notes", "package", "mounting_type", "quantity", "type"]) {
      const col = window.columnDef({ field, title: `Col ${field}` });
      // "input" + Tabulator's default "like" func = case-insensitive substring
      // filter applied live; multiple active filters AND together. That runtime
      // behaviour is Tabulator's own (the harness stubs it), so here we only
      // assert the wiring: filter present and labelled per column.
      expect(col.headerFilter).toBe("input");
      expect(col.headerFilterPlaceholder).toBe(`Filter Col ${field}…`);
      expect(col.headerFilterParams.elementAttributes["aria-label"]).toBe(
        `Filter Col ${field}`,
      );
    }
    // The row-action buttons column is not filterable.
    expect(window.actionColumn().headerFilter).toBeUndefined();
  });

  it("remembers a dragged column width across table rebuilds", () => {
    // loadTable rebuilds the columns on every filter change AND after every stock
    // write, so without this a widened column snaps back within seconds.
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    expect(window.columnDef({ field: "notes", title: "Description" }).width).toBe(260);

    window.rememberColumnWidth("notes", 640);
    expect(window.columnDef({ field: "notes", title: "Description" }).width).toBe(640);
    // Any column, not just the description one.
    window.rememberColumnWidth("param_7", 180);
    expect(window.columnDef({ field: "param_7", title: "R" }).width).toBe(180);
    // Untouched columns stay auto-sized.
    expect(window.columnDef({ field: "mpn", title: "MPN" }).width).toBeUndefined();
  });

  it("saves the width Tabulator reports when a column is resized", () => {
    // The half that makes the remembering reachable: without this subscription
    // nothing ever calls rememberColumnWidth, and the tests above would pass on a
    // feature the user can't trigger.
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    const onResized = window.Tabulator.handlers.columnResized;
    expect(onResized).toBeTypeOf("function");

    onResized({ getField: () => "notes", getWidth: () => 512 });
    expect(window.columnDef({ field: "notes", title: "Description" }).width).toBe(512);
  });

  it("ignores unusable stored widths instead of breaking the table", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    window.localStorage.setItem("shelfos.columnWidths", "not json");
    expect(window.columnDef({ field: "notes", title: "Description" }).width).toBe(260);

    window.rememberColumnWidth("notes", 0); // a collapsed drag must not stick
    window.rememberColumnWidth(undefined, 300);
    expect(window.columnDef({ field: "notes", title: "Description" }).width).toBe(260);
  });

  it("a description cell ellipsises rather than wrapping, under the real app.css", () => {
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(`<style>${css}</style><span class="cell-desc" id="d"></span>`);
    const style = dom.window.getComputedStyle(dom.window.document.getElementById("d"));
    // `display: block` is the load-bearing one: text-overflow does nothing on an
    // inline span, so without it the ellipsis would silently be the vendor's job.
    expect(style.display).toBe("block");
    expect(style.textOverflow).toBe("ellipsis");
    expect(style.overflow).toBe("hidden");
    expect(style.whiteSpace).toBe("nowrap");
  });

  it("quantity filter matches both the raw and thousands-separated number", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    const { headerFilterFunc } = window.columnDef({
      field: "quantity",
      title: "Qty",
    });
    // The cell shows the grouped form (locale-dependent); typing either the raw
    // digits or exactly what is shown should match. Derive the shown form from
    // the same toLocaleString the formatter uses, so this is not locale-brittle.
    const shown = (1234).toLocaleString();
    expect(headerFilterFunc("1234", 1234)).toBe(true);
    expect(headerFilterFunc(shown, 1234)).toBe(true);
    expect(headerFilterFunc("99", 1234)).toBe(false);
  });

  it("sorts numeric columns by magnitude, not by the display string", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    // Quantity uses Tabulator's built-in number sorter.
    expect(window.columnDef({ field: "quantity", title: "Qty" }).sorter).toBe("number");

    // A per-type number column gets a custom sorter over the raw `__n` value;
    // a text column does not.
    const num = window.columnDef({ title: "R", field: "param_5", numeric: true });
    expect(typeof num.sorter).toBe("function");
    expect(window.columnDef({ title: "Tol", field: "param_9", numeric: false }).sorter)
      .toBeUndefined();

    const row = (n) => ({ getData: () => ({ param_5__n: n }) });
    const sort = window.numericParamSorter("param_5");
    expect(sort(null, null, row(47), row(220))).toBeLessThan(0); // 47 Ω < 220 Ω
    expect(sort(null, null, row(1_000_000), row(220))).toBeGreaterThan(0); // 1 MΩ > 220 Ω
    expect(sort(null, null, row(null), row(5))).toBe(-1); // empties to one end
    expect(sort(null, null, row(null), row(null))).toBe(0);

    // A full sort of >2 rows with mixed empties orders by magnitude.
    const rows = [220, null, 47, 1_000_000, null].map(row);
    rows.sort((x, y) => sort(null, null, x, y));
    expect(rows.map((r) => r.getData().param_5__n)).toEqual([
      null,
      null,
      47,
      220,
      1_000_000,
    ]);
  });

  it("builds the type-filter query string", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    expect(window.currentTypeQuery()).toBe("");
    document.getElementById("type-filter").value = "1";
    expect(window.currentTypeQuery()).toBe("?type_id=1");
  });

  it("loadTable fetches, maps columns (+ actions) and sets the data", async () => {
    const fetchImpl = (url) =>
      url.startsWith("/web/api/components")
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              columns: [
                { title: "MPN", field: "mpn" },
                { title: "Type", field: "type" },
              ],
              data: [{ id: 1 }, { id: 2 }],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { window } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    const setColumns = vi.spyOn(window.Tabulator.prototype, "setColumns");
    const setData = vi.spyOn(window.Tabulator.prototype, "setData");

    await window.loadTable();

    expect(setColumns.mock.calls[0][0].map((c) => c.field)).toEqual([
      "mpn",
      "type",
      "actions",
    ]);
    expect(setData.mock.calls[0][0]).toEqual([{ id: 1 }, { id: 2 }]);
  });
});

describe("app.js — row actions", () => {
  it("renders the three row actions and opens the stock dialog on Add", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    const col = window.actionColumn();
    expect(col.width).toBe(200);
    const html = col.formatter();
    expect(html).toContain('data-act="add"');
    expect(html).toContain('data-act="take"');
    expect(html).toContain('data-act="details"');

    const cell = { getRow: () => ({ getData: () => ({ id: 7 }) }) };
    col.cellClick({ target: { dataset: { act: "add" } } }, cell);
    expect(document.getElementById("stock-dialog-title").textContent).toBe("Add stock");
    expect(document.getElementById("stock-form").component_id.value).toBe("7");
    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
  });

  it("boots on a read-only page, where the stock dialog isn't rendered at all", () => {
    // The dialog markup is now gated on the role, so app.js runs on a page where
    // stock_dialog.js bailed out and `openStockDialog` was never defined. The table
    // must still come up — and a read-only account has no button to reach it with.
    const { window } = loadPage(
      `<select id="type-filter"></select><div id="components-table"></div>`,
      ["shared.js", "stock_dialog.js", "app.js"],
      { role: "read-only" },
    );
    expect(window.openStockDialog).toBeUndefined();
    expect(window.actionColumn().formatter()).not.toContain('data-act="add"');
  });

  it("hides the write actions for a read-only account, keeping Details", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS, { role: "read-only" });
    const col = window.actionColumn();
    expect(col.width).toBe(100); // narrower without the Add/Take buttons
    const html = col.formatter();
    expect(html).not.toContain('data-act="add"');
    expect(html).not.toContain('data-act="take"');
    expect(html).toContain('data-act="details"');
  });

  it("opens the dialog in take mode from a Take cell click", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    window.actionColumn().cellClick(
      { target: { dataset: { act: "take" } } },
      { getRow: () => ({ getData: () => ({ id: 4 }) }) },
    );
    expect(document.getElementById("stock-dialog-title").textContent).toBe(
      "Take from stock",
    );
    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
  });

  it("navigates to the detail page on Details, without opening the dialog", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    // jsdom can't navigate (the harness swallows that error); the branch is
    // distinguished by NOT opening the stock dialog the add/take branches do.
    window.actionColumn().cellClick(
      { target: { dataset: { act: "details" } } },
      { getRow: () => ({ getData: () => ({ id: 42 }) }) },
    );
    expect(window.HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
  });

  it("ignores a cell click that hit no action button", () => {
    const { window } = loadPage(typePageFixture(), SCRIPTS);
    window.actionColumn().cellClick(
      { target: { dataset: {} } },
      { getRow: () => ({ getData: () => ({}) }) },
    );
    expect(window.HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
  });

  it("titles the dialog 'Take from stock' in take mode", () => {
    const { window, document } = loadPage(typePageFixture(), SCRIPTS);
    window.openStockDialog("take", 3);
    expect(document.getElementById("stock-dialog-title").textContent).toBe(
      "Take from stock",
    );
    expect(document.getElementById("stock-form").mode.value).toBe("take");
  });
});

describe("app.js — New Type dialog", () => {
  const openBuilder = (handles) =>
    handles.document.getElementById("new-type-btn").click();

  it("adds and removes parameter rows, toggling the empty hint", () => {
    const h = loadPage(typePageFixture(), SCRIPTS);
    openBuilder(h);
    const { document } = h;
    expect(document.getElementById("params-empty").hidden).toBe(false);

    document.getElementById("add-param").click();
    expect(document.querySelectorAll("#params .param-row").length).toBe(1);
    expect(document.getElementById("params-empty").hidden).toBe(true);

    document.querySelector(".param-remove").click();
    expect(document.querySelectorAll("#params .param-row").length).toBe(0);
    expect(document.getElementById("params-empty").hidden).toBe(false);
  });

  it("clears previously added parameter rows when reopened", () => {
    const h = loadPage(typePageFixture(), SCRIPTS);
    openBuilder(h);
    h.document.getElementById("add-param").click();
    h.document.getElementById("add-param").click();
    expect(h.document.querySelectorAll("#params .param-row").length).toBe(2);

    openBuilder(h); // resetTypeForm wipes the previous session's rows
    expect(h.document.querySelectorAll("#params .param-row").length).toBe(0);
    expect(h.document.getElementById("params-empty").hidden).toBe(false);
  });

  it("numbers sort_order by row position across multiple parameters", async () => {
    const fetchImpl = (url, opts) =>
      url === "/api/types" && opts?.method === "POST"
        ? Promise.resolve({ ok: true, json: async () => ({ id: 9, name: "cap" }) })
        : Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    const { document, fetchMock } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    openBuilder({ document });
    document.querySelector('[name="type-name"]').value = "cap";
    const fillRow = (row, name) => {
      row.querySelector('[name="p-name"]').value = name;
      row.querySelector('[name="p-label"]').value = name;
    };
    document.getElementById("add-param").click();
    document.getElementById("add-param").click();
    const rows = document.querySelectorAll(".param-row");
    fillRow(rows[0], "first");
    fillRow(rows[1], "second");
    fire(document.getElementById("type-form"), "submit");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([u, o]) => u === "/api/types" && o.method === "POST",
    );
    const params = JSON.parse(post[1].body).parameters;
    expect(params.map((p) => [p.name, p.sort_order])).toEqual([
      ["first", 0],
      ["second", 1],
    ]);
  });

  it("reveals the allowed-values field only for an enum parameter", () => {
    const h = loadPage(typePageFixture(), SCRIPTS);
    openBuilder(h);
    h.document.getElementById("add-param").click();
    const row = h.document.querySelector(".param-row");
    const enumField = row.querySelector(".param-enum");
    const dataType = row.querySelector('[name="p-data-type"]');

    expect(enumField.hidden).toBe(true);
    dataType.value = "enum";
    fire(dataType, "change");
    expect(enumField.hidden).toBe(false);
    dataType.value = "number";
    fire(dataType, "change");
    expect(enumField.hidden).toBe(true);
  });

  it("submits collected parameters, with enum values only for enum type", async () => {
    const fetchImpl = (url, opts) => {
      if (url === "/api/types" && opts?.method === "POST")
        return Promise.resolve({ ok: true, json: async () => ({ id: 9, name: "cap" }) });
      return Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    };
    const { document, fetchMock } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    openBuilder({ document });
    document.querySelector('[name="type-name"]').value = "cap";
    document.getElementById("add-param").click();
    const row = document.querySelector(".param-row");
    row.querySelector('[name="p-name"]').value = "tolerance";
    row.querySelector('[name="p-label"]').value = "Tolerance";
    const dataType = row.querySelector('[name="p-data-type"]');
    dataType.value = "enum";
    fire(dataType, "change");
    row.querySelector('[name="p-enum"]').value = "X7R, C0G ,"; // blanks dropped
    row.querySelector('[name="p-table"]').checked = true;
    fire(document.getElementById("type-form"), "submit");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([u, o]) => u === "/api/types" && o.method === "POST",
    );
    expect(JSON.parse(post[1].body)).toEqual({
      name: "cap",
      parent_id: null,
      parameters: [
        {
          name: "tolerance",
          label: "Tolerance",
          data_type: "enum",
          unit: null,
          is_table_column: true,
          is_filterable: false,
          sort_order: 0,
          enum_values: ["X7R", "C0G"],
        },
      ],
    });
  });

  it("on success adds the type to the selects, closes and reloads the table", async () => {
    const fetchImpl = (url, opts) => {
      if (url === "/api/types" && opts?.method === "POST")
        return Promise.resolve({ ok: true, json: async () => ({ id: 9, name: "cap" }) });
      return Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    };
    const { document, fetchMock } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    openBuilder({ document });
    document.querySelector('[name="type-name"]').value = "cap";
    fire(document.getElementById("type-form"), "submit");
    await tick();

    const filter = document.getElementById("type-filter");
    expect([...filter.options].some((o) => o.value === "9")).toBe(true);
    expect(filter.value).toBe("9"); // the new type becomes the active filter
    const parent = document.querySelector('[name="parent-id"]');
    expect([...parent.options].some((o) => o.value === "9")).toBe(true);
    // The table reloads after a successful create.
    expect(
      fetchMock.mock.calls.some(([u]) => u.startsWith("/web/api/components")),
    ).toBe(true);
  });

  it("keeps per-form guards: an in-flight stock POST does not block a type submit", async () => {
    let resolveStock;
    const pendingStock = new Promise((resolve) => {
      resolveStock = resolve;
    });
    const fetchImpl = (url) => {
      if (url === "/api/stock/add") return pendingStock;
      if (url === "/api/types")
        return Promise.resolve({ ok: true, json: async () => ({ id: 9, name: "cap" }) });
      return Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    };
    const { window, document, fetchMock } = loadPage(typePageFixture(), SCRIPTS, {
      fetchImpl,
    });
    // Leave a stock POST in flight (set the picker's hidden input directly since
    // this fixture's stock picker has no selectable node).
    window.openStockDialog("add", 7);
    document.querySelector("#stock-form [name='location_id']").value = "5";
    fire(document.getElementById("stock-form"), "submit");

    // A New Type submit must still go through — it has its own guard.
    document.getElementById("new-type-btn").click();
    document.querySelector('[name="type-name"]').value = "cap";
    fire(document.getElementById("type-form"), "submit");
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/types")).toBe(true);

    resolveStock({ ok: true, json: async () => ({}) });
    await tick();
  });

  it("ignores a second submit while the type create is in flight", async () => {
    let resolveFetch;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    const fetchImpl = (url) =>
      url === "/api/types"
        ? pending
        : Promise.resolve({ ok: true, json: async () => ({ columns: [], data: [] }) });
    const { document, fetchMock } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    openBuilder({ document });
    document.querySelector('[name="type-name"]').value = "cap";
    fire(document.getElementById("type-form"), "submit"); // POST in flight
    fire(document.getElementById("type-form"), "submit"); // must be ignored
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/types").length).toBe(1);
    resolveFetch({ ok: true, json: async () => ({ id: 9, name: "cap" }) });
    await tick();
  });

  it("surfaces the server error on a failed create", async () => {
    const fetchImpl = (url, opts) =>
      url === "/api/types" && opts?.method === "POST"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "name taken" }) })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { document } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    openBuilder({ document });
    document.querySelector('[name="type-name"]').value = "resistor";
    fire(document.getElementById("type-form"), "submit");
    await tick();

    const error = document.getElementById("type-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("name taken");
  });
});

describe("app.js — inherited-parameters preview", () => {
  const parentParams = (params) => (url) =>
    url === "/api/types/1/parameters"
      ? Promise.resolve({ ok: true, json: async () => params })
      : Promise.resolve({ ok: true, json: async () => ({}) });

  function pickParent(document, value) {
    const parent = document.querySelector('[name="parent-id"]');
    parent.value = value;
    fire(parent, "change");
  }

  it("prompts for a parent by default", () => {
    const { document } = loadPage(typePageFixture(), SCRIPTS);
    document.getElementById("new-type-btn").click(); // resetTypeForm -> prompt
    const hint = document.getElementById("inherited-hint");
    expect(hint.hidden).toBe(false);
    expect(hint.textContent).toMatch(/Select a parent type/);
  });

  it("lists the parent's parameters, formatting enum values", async () => {
    const params = [
      { name: "resistance", label: "Resistance", data_type: "number", unit: "Ω", enum_values: [] },
      { name: "dielectric", label: "Dielectric", data_type: "enum", unit: null, enum_values: ["X7R", "C0G"] },
    ];
    const { document } = loadPage(typePageFixture(), SCRIPTS, {
      fetchImpl: parentParams(params),
    });
    document.getElementById("new-type-btn").click();
    pickParent(document, "1");
    await tick();

    expect(document.getElementById("inherited-hint").hidden).toBe(true);
    const items = document.querySelectorAll("#inherited-list .inherited-item");
    expect(items.length).toBe(2);
    expect(items[1].innerHTML).toContain("(X7R, C0G)");
  });

  it("hints when the parent defines no parameters", async () => {
    const { document } = loadPage(typePageFixture(), SCRIPTS, {
      fetchImpl: parentParams([]),
    });
    document.getElementById("new-type-btn").click();
    pickParent(document, "1");
    await tick();
    const hint = document.getElementById("inherited-hint");
    expect(hint.hidden).toBe(false);
    expect(hint.textContent).toMatch(/defines no parameters/);
  });

  it("surfaces a load failure distinctly from an empty parent", async () => {
    const fetchImpl = (url) =>
      url === "/api/types/1/parameters"
        ? Promise.resolve({ ok: false, json: async () => ({}) })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { document } = loadPage(typePageFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("new-type-btn").click();
    pickParent(document, "1");
    await tick();
    expect(document.getElementById("inherited-hint").textContent).toMatch(
      /Could not load inherited/,
    );
  });

  it("ignores a superseded slow response (monotonic request id)", async () => {
    const slow = [{ name: "old", label: "Old", data_type: "text", unit: null, enum_values: [] }];
    const fast = [{ name: "new", label: "New", data_type: "text", unit: null, enum_values: [] }];
    const fetchImpl = (url) => {
      if (url === "/api/types/1/parameters")
        return new Promise((resolve) =>
          setTimeout(() => resolve({ ok: true, json: async () => slow }), 30),
        );
      if (url === "/api/types/2/parameters")
        return Promise.resolve({ ok: true, json: async () => fast });
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { document } = loadPage(
      typePageFixture([{ id: 1, name: "a" }, { id: 2, name: "b" }]),
      SCRIPTS,
      { fetchImpl },
    );
    document.getElementById("new-type-btn").click();
    pickParent(document, "1"); // slow request
    pickParent(document, "2"); // supersedes it
    await tick();
    await new Promise((resolve) => setTimeout(resolve, 50)); // let the slow one land

    const names = [...document.querySelectorAll("#inherited-list .ip-name")].map(
      (n) => n.textContent,
    );
    expect(names).toEqual(["new"]); // the stale slow response was dropped
  });
});

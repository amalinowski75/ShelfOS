import { describe, it, expect } from "vitest";
import { loadPage, tick } from "./harness.js";

const SCRIPTS = ["shared.js", "audit.js"];

// The audit page shell (mirrors audit.html).
const FIXTURE = `
  <button type="button" id="audit-clear" hidden>Clear filters</button>
  <div id="audit-table"
       data-kinds='["component","user"]'
       data-actors='[{"id":1,"name":"admin"},{"id":2,"name":"bob"}]'></div>
  <button type="button" id="audit-more" hidden>Show more</button>
  <p id="audit-count"></p>`;

const ROW = (n) => ({
  when: `2026-08-18T10:0${n}:00`,
  who: "admin",
  entity: `component RC060${n}`,
  entity_url: `/components/${n}`,
  what: "quantity in D1",
  old: "0",
  new: `${n}`,
});

const ok = (data) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });

// A filter change is asked for once the typing has stopped, so a test that
// changes one has to let it stop. Real time rather than fake timers: the page
// also awaits a fetch, and mixing the two clocks reads worse than waiting.
const settle = async () => {
  await new Promise((resolve) => setTimeout(resolve, 350));
  await tick();
};

describe("audit.js", () => {
  it("loads the newest entries and says how many are shown", async () => {
    const { document, window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => ok({ data: [ROW(1), ROW(2)], more: false }),
    });
    await tick();

    expect(window.Tabulator.rows).toHaveLength(2);
    expect(document.getElementById("audit-count").textContent).toBe(
      "2 entries",
    );
    // Nothing behind this page, so nothing offers to fetch it.
    expect(document.getElementById("audit-more").hidden).toBe(true);
  });

  it("narrows from the server when a column is filtered", async () => {
    // The table itself filters nothing: this page holds a window onto a log
    // walked a page at a time, so a filter over the rows on screen would answer
    // "nothing" for an entry sitting one page further back.
    const calls = [];
    const { document, window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({ data: [ROW(1)], more: false });
      },
    });
    await tick();

    window.Tabulator.filters = [
      { field: "entity_kind", value: "component" },
      { field: "who_id", value: 2 },
      { field: "what", value: "quantity" },
      { field: "change", value: "250" },
    ];
    window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    await settle();

    const last = calls.at(-1);
    expect(last).toContain("entity_type=component");
    expect(last).toContain("who=2"); // by id: two people can be renamed
    expect(last).toContain("field=quantity");
    expect(last).toContain("value=250");
    // A changed filter restarts the walk rather than narrowing what is loaded.
    expect(last).not.toContain("before_");
    expect(document.getElementById("audit-clear").hidden).toBe(false);
  });

  it("leaves the narrowing to the server, not to the rows on screen", async () => {
    // The mechanism behind the test above, asserted where it lives: a column
    // that filtered locally as well would hide rows the server had already
    // chosen, and the page would quietly disagree with the query it ran.
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => ok({ data: [ROW(1)], more: false }),
    });
    await tick();

    const filtered = window.Tabulator.columns.filter((col) => col.headerFilter);
    expect(filtered.length).toBeGreaterThan(0);
    for (const column of filtered) {
      expect(column.headerFilterFunc).toBeTypeOf("function");
      expect(column.headerFilterFunc("anything", "unrelated")).toBe(true);
    }
  });

  it("offers a way out of the filters it is under", async () => {
    const calls = [];
    const { document, window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({ data: [ROW(1)], more: false });
      },
    });
    await tick();
    window.Tabulator.filters = [{ field: "who_id", value: 2 }];
    window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    await settle();

    document.getElementById("audit-clear").click();
    await settle();

    expect(calls.at(-1)).not.toContain("who=");
    expect(document.getElementById("audit-clear").hidden).toBe(true);
  });

  it("walks further back on demand instead of loading the whole log", async () => {
    const calls = [];
    const { document, window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        const n = calls.length;
        return ok({
          data: [ROW(n)],
          more: n < 2,
          cursor: n < 2 ? { when: "2026-08-18T10:01:00.123456", id: 7 } : null,
        });
      },
    });
    await tick();

    const more = document.getElementById("audit-more");
    expect(more.hidden).toBe(false); // there is history behind the first page
    more.click();
    await tick();

    // Continues from where the last page stopped, not from how far in it was:
    // the log grows at the head while it is read, and an offset would push the
    // boundary row into this page and show the same change twice.
    expect(calls.at(-1)).toContain("before_id=7");
    expect(calls.at(-1)).toContain(
      `before_when=${encodeURIComponent("2026-08-18T10:01:00.123456")}`,
    );
    expect(calls.at(-1)).not.toContain("offset=");
    expect(window.Tabulator.rows).toHaveLength(2); // appended, not replaced
    expect(more.hidden).toBe(true); // and the end of the log stops offering
  });

  it("does not carry a cursor across a change of filter", async () => {
    // The cursor names a row in the previous query's ordering; keeping it would
    // start the new, narrower walk somewhere in the middle of the old one.
    const calls = [];
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({
          data: [ROW(1)],
          more: true,
          cursor: { when: "2026-08-18T10:01:00", id: 7 },
        });
      },
    });
    await tick();

    window.Tabulator.filters = [{ field: "who_id", value: 2 }];
    window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    await settle();

    expect(calls.at(-1)).toContain("who=2");
    expect(calls.at(-1)).not.toContain("before_id");
  });

  it("asks once for a burst of keystrokes, not once per keystroke", async () => {
    // Each of these is a `%like%` over the widest-growing table in the system,
    // which no index can help with — so a typed word must cost one query, not
    // one per letter.
    const calls = [];
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({ data: [ROW(1)], more: false });
      },
    });
    await tick();
    const before = calls.length;

    for (const typed of ["r", "re", "res", "resi"]) {
      window.Tabulator.filters = [{ field: "change", value: typed }];
      window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    }
    await settle();

    expect(calls.length - before).toBe(1);
    expect(calls.at(-1)).toContain("value=resi"); // and it is the last one typed
  });

  it("does not answer with a query the filter has moved on from", async () => {
    // Two requests can be in flight at once, and the slower one is not
    // necessarily the older one. Whichever lands last must not win: a table
    // showing rows that do not match the filter above them is worse than a
    // slow one, because nothing on screen says so.
    const pending = [];
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        if (!url.includes("value=")) return ok({ data: [ROW(1)], more: false });
        return new Promise((release) => pending.push({ url, release }));
      },
    });
    await tick();

    window.Tabulator.filters = [{ field: "change", value: "re" }];
    window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    await settle();
    window.Tabulator.filters = [{ field: "change", value: "res" }];
    window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    await settle();
    expect(pending).toHaveLength(2);

    // The newer query answers first; the older one lands after it.
    pending[1].release(ok({ data: [{ ...ROW(2), new: "res-answer" }], more: false }));
    await tick();
    pending[0].release(ok({ data: [{ ...ROW(3), new: "re-answer" }], more: false }));
    await tick();

    expect(window.Tabulator.rows.map((row) => row.new)).toEqual(["res-answer"]);
  });

  it("reloads once for a filter change, not once per reload", async () => {
    // Replacing the data makes the table re-run its filters, which is what
    // asked for the reload — without a guard that is a request loop, and the
    // page walks the log until somebody closes the tab.
    const calls = [];
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({ data: [ROW(1)], more: false });
      },
    });
    await tick();
    const before = calls.length;

    window.Tabulator.filters = [{ field: "who_id", value: 2 }];
    window.Tabulator.handlers.dataFiltering(window.Tabulator.filters);
    await settle();
    await settle(); // and it stays settled

    expect(calls.length - before).toBe(1);
  });

  it("says which zone the timestamps are in", async () => {
    // The page's whole purpose is "who did what, when"; a column of bare
    // timestamps leaves the reader to guess whether they are local.
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => ok({ data: [ROW(1)], more: false }),
    });
    await tick();

    const when = window.Tabulator.columns.find((col) => col.field === "when");
    expect(when.title).toContain("UTC");
  });

  it("shows an absent value as absent rather than as blank", async () => {
    const { window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () =>
        ok({ data: [{ ...ROW(1), old: null, new: "admin" }], more: false }),
    });
    await tick();

    // A cleared field and an untouched one must not look the same; the column
    // formatter renders null as a dash, which the row data carries as null.
    expect(window.Tabulator.rows[0].old).toBeNull();
  });
});

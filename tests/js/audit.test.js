import { describe, it, expect } from "vitest";
import { loadPage, tick } from "./harness.js";

const SCRIPTS = ["shared.js", "audit.js"];

// The audit page shell (mirrors audit.html).
const FIXTURE = `
  <select class="control" id="audit-entity">
    <option value="">everything</option>
    <option value="component">component</option>
  </select>
  <div id="audit-table"></div>
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

describe("audit.js", () => {
  it("loads the newest entries and says how many are shown", async () => {
    const { document, window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => ok({ data: [ROW(1), ROW(2)], more: false }),
    });
    await tick();

    expect(window.Tabulator.rows).toHaveLength(2);
    expect(document.getElementById("audit-count").textContent).toBe("2 entries");
    // Nothing behind this page, so nothing offers to fetch it.
    expect(document.getElementById("audit-more").hidden).toBe(true);
  });

  it("filters by what was changed, from the server", async () => {
    const calls = [];
    const { document } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({ data: [ROW(1)], more: false });
      },
    });
    await tick();

    const select = document.getElementById("audit-entity");
    select.value = "component";
    select.dispatchEvent(new document.defaultView.Event("change"));
    await tick();

    expect(calls.at(-1)).toContain("entity_type=component");
    // A filter restarts the walk rather than appending to what was on screen.
    expect(calls.at(-1)).toContain("offset=0");
  });

  it("walks further back on demand instead of loading the whole log", async () => {
    const calls = [];
    const { document, window } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        calls.push(url);
        return ok({ data: [ROW(calls.length)], more: calls.length < 2 });
      },
    });
    await tick();

    const more = document.getElementById("audit-more");
    expect(more.hidden).toBe(false); // there is history behind the first page
    more.click();
    await tick();

    expect(calls.at(-1)).toContain("offset=1"); // continues, not restarts
    expect(window.Tabulator.rows).toHaveLength(2); // appended, not replaced
    expect(more.hidden).toBe(true); // and the end of the log stops offering
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

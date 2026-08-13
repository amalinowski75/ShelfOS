import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

const SCRIPTS = ["shared.js", "bulk_location_dialog.js"];

// Mirrors _bulk_location_dialog.html: the dialog, the level-row template and the
// page's "Generate hierarchy" trigger.
function bulkDialogFixture() {
  return `
    <button id="generate-locations-btn"></button>
    <dialog id="bulk-location-dialog">
      <form id="bulk-location-form">
        <select name="parent_id">
          <option value="">None (top level)</option>
          <option value="3">Lab</option>
        </select>
        <div id="bulk-levels"></div>
        <button type="button" id="bulk-add-level"></button>
        <p id="bulk-total"></p>
        <div id="bulk-preview" hidden><ul id="bulk-preview-list"></ul></div>
        <p id="bulk-error" hidden></p>
        <button type="button" id="bulk-preview-btn"></button>
        <button type="submit" id="bulk-create-btn"></button>
      </form>
    </dialog>
    <template id="bulk-level-template">
      <div class="bulk-level">
        <span class="bulk-depth" aria-hidden="true">&#8627;</span>
        <select name="level-type">
          <option value="shelf">shelf</option>
          <option value="drawer">drawer</option>
        </select>
        <input name="level-count" type="number" min="1" max="100" value="1" />
        <input name="level-pattern" />
        <button type="button" class="bulk-level-remove"></button>
      </div>
    </template>`;
}

function open(options) {
  const handles = loadPage(bulkDialogFixture(), SCRIPTS, options);
  handles.document.getElementById("generate-locations-btn").click();
  handles.rows = () => handles.document.querySelectorAll(".bulk-level");
  handles.setLevel = (index, { type, count, pattern }) => {
    const row = handles.rows()[index];
    if (type !== undefined) row.querySelector('[name="level-type"]').value = type;
    if (count !== undefined) {
      const input = row.querySelector('[name="level-count"]');
      input.value = String(count);
      input.dispatchEvent(
        new handles.window.Event("input", { bubbles: true }),
      );
    }
    if (pattern !== undefined)
      row.querySelector('[name="level-pattern"]').value = pattern;
  };
  return handles;
}

describe("bulk_location_dialog.js", () => {
  it("opens with one level row and grows/shrinks the list", () => {
    const h = open();
    expect(h.rows().length).toBe(1);
    h.document.getElementById("bulk-add-level").click();
    h.document.getElementById("bulk-add-level").click();
    expect(h.rows().length).toBe(3);
    h.rows()[1].querySelector(".bulk-level-remove").click();
    expect(h.rows().length).toBe(2);
  });

  it("staggers the rows so nesting is visible", () => {
    const h = open();
    h.document.getElementById("bulk-add-level").click();
    h.document.getElementById("bulk-add-level").click();
    const rows = [...h.rows()];
    expect(rows.map((row) => row.style.marginLeft)).toEqual([
      "0px",
      "18px",
      "36px",
    ]);
    // The ↳ marker only makes sense under something.
    expect(rows[0].querySelector(".bulk-depth").hidden).toBe(true);
    expect(rows[1].querySelector(".bulk-depth").hidden).toBe(false);
    // Removing the middle level re-staggers what is left.
    rows[1].querySelector(".bulk-level-remove").click();
    expect([...h.rows()].map((row) => row.style.marginLeft)).toEqual([
      "0px",
      "18px",
    ]);
  });

  it("shows the multiplication as a running total", () => {
    const h = open();
    h.document.getElementById("bulk-add-level").click();
    h.setLevel(0, { count: 4 });
    h.setLevel(1, { count: 6 });
    // 4 shelves + 4×6 drawers.
    expect(h.document.getElementById("bulk-total").textContent).toBe(
      "4 × 6 → 28 locations",
    );
  });

  it("follows the chosen type in the pattern placeholder", () => {
    const h = open();
    const row = h.rows()[0];
    const select = row.querySelector('[name="level-type"]');
    select.value = "drawer";
    select.dispatchEvent(new h.window.Event("change", { bubbles: true }));
    expect(row.querySelector('[name="level-pattern"]').placeholder).toBe(
      "Drawer {n}",
    );
  });

  it("previews via a dry run and renders the sample paths", async () => {
    const h = open({
      fetchImpl: () =>
        Promise.resolve({
          ok: true,
          json: async () => ({
            total: 28,
            created: 0,
            sample_paths: ["Lab / Shelf 1 / Drawer 1"],
          }),
        }),
    });
    h.setLevel(0, { count: 4, pattern: "Shelf {n}" });
    h.document.querySelector('[name="parent_id"]').value = "3";
    h.document.getElementById("bulk-preview-btn").click();
    await tick();

    const [url, opts] = h.fetchMock.mock.calls[0];
    expect(url).toBe("/api/locations/bulk");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({
      parent_id: 3,
      levels: [{ type: "shelf", count: 4, name_pattern: "Shelf {n}" }],
      dry_run: true,
    });
    expect(h.document.getElementById("bulk-preview").hidden).toBe(false);
    expect(
      h.document.getElementById("bulk-preview-list").textContent,
    ).toContain("Lab / Shelf 1 / Drawer 1");
    expect(
      h.document.getElementById("bulk-preview-list").textContent,
    ).toContain("28 in total");
  });

  it("creates with dry_run false and a null pattern when blank", async () => {
    const h = open();
    h.setLevel(0, { type: "drawer", count: 2 });
    h.document
      .getElementById("bulk-location-form")
      .dispatchEvent(
        new h.window.Event("submit", { cancelable: true, bubbles: true }),
      );
    await tick();
    expect(JSON.parse(h.fetchMock.mock.calls[0][1].body)).toEqual({
      parent_id: null,
      levels: [{ type: "drawer", count: 2, name_pattern: null }],
      dry_run: false,
    });
  });

  it("surfaces a rejected plan in the dialog", async () => {
    const h = open({
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          json: async () => ({ detail: "these names already exist" }),
        }),
    });
    h.document.getElementById("bulk-preview-btn").click();
    await tick();
    const error = h.document.getElementById("bulk-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("these names already exist");
  });

  it("surfaces a network failure instead of an unhandled rejection", async () => {
    const h = open({ fetchImpl: () => Promise.reject(new Error("down")) });
    h.document
      .getElementById("bulk-location-form")
      .dispatchEvent(
        new h.window.Event("submit", { cancelable: true, bubbles: true }),
      );
    await tick();
    expect(h.document.getElementById("bulk-error").hidden).toBe(false);
  });

  it("does nothing at all on a page without the dialog", () => {
    expect(() => loadPage(`<p>elsewhere</p>`, SCRIPTS)).not.toThrow();
  });
});

import { describe, it, expect } from "vitest";
import { loadPage, tick } from "./harness.js";

const SCRIPTS = ["shared.js", "location_tree.js"];
// location_dialog.js must load first so window.openLocationDialog exists when
// location_tree.js enhances the picker (it reads it once, at enhance time).
const SCRIPTS_WITH_DIALOG = ["shared.js", "location_dialog.js", "location_tree.js"];

// A picker whose menu carries the inline "+ New location" button, alongside the
// shared New Location dialog it drives.
function creatableFixture() {
  return `
    <form>
    <div class="loc-picker">
      <input type="hidden" name="location_id" value="" />
      <button type="button" class="loc-picker-toggle">
        <span class="loc-picker-label">Select a location…</span>
      </button>
      <div class="loc-picker-menu" hidden>
        <button type="button" class="loc-picker-new" hidden>+ New location</button>
        <ul class="loc-picker-list">
          <li>
            <div class="loc-picker-row">
              <span class="loc-picker-caret-spacer"></span>
              <button type="button" class="loc-picker-node"
                      data-loc-id="1" data-loc-path="Lab">Lab</button>
            </div>
          </li>
        </ul>
      </div>
    </div>
    </form>
    <dialog id="location-dialog"><form id="location-form">
      <select name="type"><option value="rack">rack</option></select>
      <input name="name" />
      <select name="parent_id">
        <option value="">None (top level)</option>
        <option value="1">Lab</option>
      </select>
      <p id="location-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
}

function submit(document, formId) {
  document.getElementById(formId).dispatchEvent(
    new document.defaultView.Event("submit", { bubbles: true, cancelable: true }),
  );
}

// Mirrors the _location_picker.html macro output: a two-level tree.
function pickerFixture() {
  return `
    <form>
    <div class="loc-picker">
      <input type="hidden" name="location_id" value="" />
      <button type="button" class="loc-picker-toggle">
        <span class="loc-picker-label">Select a location…</span>
      </button>
      <div class="loc-picker-menu" hidden>
        <ul class="loc-picker-list">
          <li>
            <div class="loc-picker-row">
              <button type="button" class="loc-picker-caret" aria-expanded="false"></button>
              <button type="button" class="loc-picker-node"
                      data-loc-id="1" data-loc-path="Lab">Lab</button>
            </div>
            <div class="loc-picker-children" hidden>
              <ul class="loc-picker-list"><li>
                <div class="loc-picker-row">
                  <span class="loc-picker-caret-spacer"></span>
                  <button type="button" class="loc-picker-node"
                          data-loc-id="2" data-loc-path="Lab / Rack A">Rack A</button>
                </div>
              </li></ul>
            </div>
          </li>
        </ul>
      </div>
    </div>
    </form>`;
}

describe("location_tree.js", () => {
  it("opens the menu, expands a branch and selects a nested node", () => {
    const { document } = loadPage(pickerFixture(), SCRIPTS);
    const menu = document.querySelector(".loc-picker-menu");
    expect(menu.hidden).toBe(true);

    document.querySelector(".loc-picker-toggle").click();
    expect(menu.hidden).toBe(false);

    const caret = document.querySelector(".loc-picker-caret");
    const children = document.querySelector(".loc-picker-children");
    expect(children.hidden).toBe(true);
    caret.click();
    expect(children.hidden).toBe(false);
    expect(caret.getAttribute("aria-expanded")).toBe("true");

    document.querySelector('.loc-picker-node[data-loc-id="2"]').click();
    expect(document.querySelector('input[name="location_id"]').value).toBe("2");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "Lab / Rack A",
    );
    expect(menu.hidden).toBe(true); // selecting closes the menu
  });

  it("exposes setValue and reset for programmatic control", () => {
    const { document } = loadPage(pickerFixture(), SCRIPTS);
    const picker = document.querySelector(".loc-picker");
    const input = document.querySelector('input[name="location_id"]');
    const label = document.querySelector(".loc-picker-label");

    picker.setValue(1);
    expect(input.value).toBe("1");
    expect(label.textContent.trim()).toBe("Lab");

    picker.reset();
    expect(input.value).toBe("");
    expect(label.textContent.trim()).toBe("Select a location…");
  });

  it("Escape closes the menu and cancels the default (dialog close)", () => {
    const { document } = loadPage(pickerFixture(), SCRIPTS);
    const menu = document.querySelector(".loc-picker-menu");
    document.querySelector(".loc-picker-toggle").click();
    expect(menu.hidden).toBe(false);

    const event = new document.defaultView.KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    document.querySelector(".loc-picker-node").dispatchEvent(event);
    expect(menu.hidden).toBe(true);
    // preventDefault stops the native <dialog> from closing too.
    expect(event.defaultPrevented).toBe(true);
  });

  it("setValue with an unknown id clears the selection", () => {
    const { document } = loadPage(pickerFixture(), SCRIPTS);
    document.querySelector(".loc-picker").setValue(999);
    expect(document.querySelector('input[name="location_id"]').value).toBe("");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "Select a location…",
    );
  });

  it("closes the menu when clicking outside the picker", () => {
    const { document } = loadPage(pickerFixture() + "<div id=out></div>", SCRIPTS);
    const menu = document.querySelector(".loc-picker-menu");
    document.querySelector(".loc-picker-toggle").click();
    expect(menu.hidden).toBe(false);
    document.getElementById("out").click();
    expect(menu.hidden).toBe(true);
  });
});

describe("location_tree.js inline New location", () => {
  const created = (extra) => (url, opts) =>
    url === "/api/locations" && opts?.method === "POST"
      ? Promise.resolve({
          ok: true,
          json: async () => ({ id: 7, name: "Rack A", type: "rack", ...extra }),
        })
      : Promise.resolve({ ok: true, json: async () => ({}) });

  it("reveals the inline button only when the New location dialog is present", () => {
    const withDialog = loadPage(creatableFixture(), SCRIPTS_WITH_DIALOG);
    expect(withDialog.document.querySelector(".loc-picker-new").hidden).toBe(false);

    // Same markup, but location_dialog.js not loaded -> no window.openLocationDialog.
    const without = loadPage(creatableFixture(), SCRIPTS);
    expect(without.document.querySelector(".loc-picker-new").hidden).toBe(true);
  });

  it("creates a location inline and selects it, pathed under its parent", async () => {
    const { document } = loadPage(creatableFixture(), SCRIPTS_WITH_DIALOG, {
      fetchImpl: created({ parent_id: 1 }),
    });
    document.querySelector(".loc-picker-toggle").click();
    document.querySelector(".loc-picker-new").click();
    document.querySelector('#location-form [name="name"]').value = "Rack A";
    document.querySelector('#location-form [name="parent_id"]').value = "1";
    submit(document, "location-form");
    await tick();

    expect(document.querySelector('input[name="location_id"]').value).toBe("7");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "Lab / Rack A",
    );
    const node = document.querySelector('.loc-picker-node[data-loc-id="7"]');
    expect(node).not.toBeNull();
    expect(node.dataset.locPath).toBe("Lab / Rack A");
  });

  it("labels a top-level inline location with just its name", async () => {
    const { document } = loadPage(creatableFixture(), SCRIPTS_WITH_DIALOG, {
      fetchImpl: created({ parent_id: null }),
    });
    document.querySelector(".loc-picker-toggle").click();
    document.querySelector(".loc-picker-new").click();
    document.querySelector('#location-form [name="name"]').value = "Rack A";
    submit(document, "location-form");
    await tick();

    expect(document.querySelector('input[name="location_id"]').value).toBe("7");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "Rack A",
    );
  });
});

import { describe, it, expect } from "vitest";
import { loadPage } from "./harness.js";

const SCRIPTS = ["shared.js", "location_tree.js"];

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

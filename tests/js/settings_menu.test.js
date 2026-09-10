import { describe, it, expect } from "vitest";
import { loadPage, settingsMenuFixture } from "./harness.js";

const SCRIPTS = ["settings_menu.js"];

function load() {
  const page = loadPage(settingsMenuFixture(), SCRIPTS);
  return {
    ...page,
    button: page.document.getElementById("settings-btn"),
    menu: page.document.getElementById("settings-menu"),
  };
}

describe("settings_menu.js", () => {
  it("starts closed", () => {
    const { menu, button } = load();
    expect(menu.hidden).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens on the button and closes on a second press", () => {
    const { menu, button } = load();
    button.click();
    expect(menu.hidden).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    button.click();
    expect(menu.hidden).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes when an item inside it is chosen", () => {
    const { document, menu, button } = load();
    button.click();
    document.getElementById("change-password-btn").click();
    expect(menu.hidden).toBe(true);
  });

  it("closes on a click elsewhere on the page", () => {
    const { document, menu, button } = load();
    button.click();
    document.body.click();
    expect(menu.hidden).toBe(true);
  });

  it("closes on Escape and puts the focus back on the button", () => {
    const { window, document, menu, button } = load();
    button.click();
    document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(button);
  });
});

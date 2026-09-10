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

  it("closes when an entry inside it is chosen", () => {
    const { document, menu, button } = load();
    button.click();
    document.getElementById("change-password-btn").click();
    expect(menu.hidden).toBe(true);
  });

  it("stays open for a click on the panel's padding, beside every entry", () => {
    const { menu, button } = load();
    button.click();
    menu.click();
    expect(menu.hidden).toBe(false);
  });

  it("lets a click on the button reach the rest of the page", () => {
    const { document, button } = load();
    const seen = [];
    document.addEventListener("click", (event) => seen.push(event.target.id));
    button.click();
    // Other widgets close themselves from a document-level click handler; one
    // that never arrives would leave them open.
    expect(seen).toEqual(["settings-btn"]);
  });

  it("closes on a click elsewhere on the page", () => {
    const { document, menu, button } = load();
    button.click();
    document.body.click();
    expect(menu.hidden).toBe(true);
  });

  it("closes on Escape and puts the focus back on the button", () => {
    const { window, document, menu, button } = load();
    button.focus();
    button.click();
    document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(button);
  });

  it("leaves the focus alone on Escape when something else has taken it", () => {
    const { window, document, menu, button } = load();
    button.click();
    // The invoice page blurs the focused control on Escape (capture phase) to
    // hand the keyboard back to the barcode collector; grabbing it for the gear
    // afterwards would disarm the scanner again.
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    elsewhere.focus();

    document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );

    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(elsewhere);
  });

  it("closes when the focus tabs out of it", () => {
    const { window, document, menu, button } = load();
    button.click();
    const beyond = document.createElement("button");
    document.body.append(beyond);

    menu.querySelector("a").dispatchEvent(
      new window.FocusEvent("focusout", { bubbles: true, relatedTarget: beyond }),
    );

    expect(menu.hidden).toBe(true);
  });

  it("stays open while the focus moves between its own entries", () => {
    const { window, menu, button } = load();
    button.click();
    const [first, second] = menu.querySelectorAll("a");

    first.dispatchEvent(
      new window.FocusEvent("focusout", { bubbles: true, relatedTarget: second }),
    );

    expect(menu.hidden).toBe(false);
  });
});

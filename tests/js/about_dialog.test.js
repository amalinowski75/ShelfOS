import { describe, it, expect } from "vitest";
import { loadPage } from "./harness.js";

const SCRIPTS = ["about_dialog.js"];

// The Settings menu entry and the dialog, as base.html renders them.
const ABOUT = `
  <button type="button" id="about-btn">About</button>
  <dialog id="about-dialog"><article><p>ShelfOS</p></article></dialog>`;

describe("about_dialog.js", () => {
  it("opens the About dialog from the Settings menu", () => {
    const { window, document } = loadPage(ABOUT, SCRIPTS);
    const showModal = window.HTMLDialogElement.prototype.showModal;
    expect(showModal).not.toHaveBeenCalled();
    document.getElementById("about-btn").click();
    expect(showModal).toHaveBeenCalledTimes(1);
  });

  it("does nothing on a page without the menu (signed out)", () => {
    expect(() => loadPage("<p>Sign in</p>", SCRIPTS)).not.toThrow();
  });
});

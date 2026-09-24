import { describe, it, expect } from "vitest";
import { loadPage } from "./harness.js";

const SCRIPTS = ["theme.js"];
const KEY = "shelfos-theme";

// The picker as base.html renders it inside the Settings menu.
const PICKER = `
  <label class="settings-theme">Theme
    <select id="theme-select">
      <option value="">System</option>
      <option value="light">Light</option>
      <option value="dark">Dark</option>
      <option value="dim">Dim</option>
    </select>
  </label>`;

// theme.js runs from <head>, before the body exists, so it wires the picker on
// DOMContentLoaded — which jsdom fires after loadPage has returned.
async function load(body = PICKER, storage = {}) {
  const page = loadPage(body, SCRIPTS, { localStorage: storage });
  await contentLoaded(page.document);
  return {
    ...page,
    root: page.document.documentElement,
    picker: page.document.getElementById("theme-select"),
  };
}

function contentLoaded(document) {
  if (document.readyState !== "loading") return Promise.resolve();
  return new Promise((resolve) =>
    document.addEventListener("DOMContentLoaded", resolve, { once: true }),
  );
}

function choose(picker, value) {
  picker.value = value;
  picker.dispatchEvent(new picker.ownerDocument.defaultView.Event("change"));
}

describe("theme.js", () => {
  it("leaves the theme to the OS when nothing was chosen", async () => {
    const { root, picker } = await load();
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(picker.value).toBe("");
  });

  it("puts a remembered choice on <html> and shows it in the picker", async () => {
    const { root, picker } = await load(PICKER, { [KEY]: "dim" });
    expect(root.dataset.theme).toBe("dim");
    expect(picker.value).toBe("dim");
  });

  it("applies a remembered choice on a page with no picker (signed out)", async () => {
    const { root } = await load("<p>Sign in</p>", { [KEY]: "dark" });
    expect(root.dataset.theme).toBe("dark");
  });

  it("ignores a stored value that is not a theme", async () => {
    const { root, picker } = await load(PICKER, { [KEY]: "neon" });
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(picker.value).toBe("");
  });

  it("switches the page and remembers the choice", async () => {
    const { window, root, picker } = await load();
    choose(picker, "dark");
    expect(root.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(KEY)).toBe("dark");
  });

  it("goes back to the OS on System, forgetting the choice", async () => {
    const { window, root, picker } = await load(PICKER, { [KEY]: "light" });
    choose(picker, "");
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(KEY)).toBe(null);
  });

  it("wires a picker the parser reaches only after the script has run", async () => {
    // In base.html theme.js sits in <head>, before the Settings menu exists; the
    // harness injects it after the body. Load it with no picker, add the picker
    // before DOMContentLoaded — as the parser would — and only then let it fire.
    const page = loadPage("<p>Before the menu</p>", SCRIPTS);
    expect(page.document.readyState).toBe("loading");
    page.document.body.insertAdjacentHTML("beforeend", PICKER);
    await contentLoaded(page.document);

    const picker = page.document.getElementById("theme-select");
    choose(picker, "dim");
    expect(page.document.documentElement.dataset.theme).toBe("dim");
  });

  it("follows the OS and keeps the picker working when storage is blocked", async () => {
    const { root, picker } = await load(PICKER, "throws");
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(picker.value).toBe("");
    choose(picker, "dark");
    expect(root.dataset.theme).toBe("dark");
  });

  it("still switches this page when storage refuses the write", async () => {
    const { window, root, picker } = await load();
    Object.defineProperty(window, "localStorage", {
      get() {
        throw new window.DOMException("blocked", "SecurityError");
      },
    });
    choose(picker, "dim");
    expect(root.dataset.theme).toBe("dim");
  });
});

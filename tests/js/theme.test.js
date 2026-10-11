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
      <option value="warm">Warm</option>
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

// What another tab's write looks like to this one.
function storageEvent(window, key, newValue) {
  window.dispatchEvent(new window.StorageEvent("storage", { key, newValue }));
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

  it("follows a choice made in another tab", async () => {
    const { window, root, picker } = await load();
    window.localStorage.setItem(KEY, "dim");
    storageEvent(window, KEY, "dim");
    expect(root.dataset.theme).toBe("dim");
    expect(picker.value).toBe("dim");

    window.localStorage.removeItem(KEY);
    storageEvent(window, KEY, null);
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(picker.value).toBe("");
  });

  it("follows another tab on a page with no picker (signed out)", async () => {
    const { window, root } = await load("<p>Sign in</p>");
    window.localStorage.setItem(KEY, "dark");
    storageEvent(window, KEY, "dark");
    expect(root.dataset.theme).toBe("dark");
  });

  it("ignores another tab's change to some other key", async () => {
    const { window, root } = await load(PICKER, { [KEY]: "light" });
    window.localStorage.removeItem(KEY); // as if cleared behind our back
    storageEvent(window, "components-columns", "[]");
    expect(root.dataset.theme).toBe("light");
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

  describe("the phone's browser bar", () => {
    // The page's ground per theme, as app.css would give it, and the meta
    // base.html carries with Light's --bg in it.
    const GROUNDS = `
      <style>
        body { background-color: rgb(246, 247, 249); }
        :root[data-theme="dark"] body { background-color: rgb(10, 17, 32); }
      </style>
      <meta name="theme-color" content="#f6f7f9">`;
    const bar = (document) =>
      document.querySelector('meta[name="theme-color"]').getAttribute("content");

    it("takes the page's ground once the page is up", async () => {
      const { document } = await load(GROUNDS + PICKER, { [KEY]: "dark" });
      expect(bar(document)).toBe("rgb(10, 17, 32)");
    });

    it("paints once the stylesheet the parser reaches later is in", async () => {
      // In base.html theme.js runs in <head>, with no body and no app.css yet:
      // nothing to read then. Give it the ground only before DOMContentLoaded.
      const page = loadPage('<meta name="theme-color" content="#f6f7f9">', SCRIPTS, {
        localStorage: { [KEY]: "dark" },
      });
      expect(page.document.readyState).toBe("loading");
      page.document.body.insertAdjacentHTML("beforeend", GROUNDS.split("<meta")[0]);
      await contentLoaded(page.document);
      expect(bar(page.document)).toBe("rgb(10, 17, 32)");
    });

    it("follows a theme picked here", async () => {
      const { document, picker } = await load(GROUNDS + PICKER);
      expect(bar(document)).toBe("rgb(246, 247, 249)");
      choose(picker, "dark");
      expect(bar(document)).toBe("rgb(10, 17, 32)");
      choose(picker, "");
      expect(bar(document)).toBe("rgb(246, 247, 249)");
    });

    it("follows a theme picked in another tab", async () => {
      const { window, document } = await load(GROUNDS + PICKER);
      window.localStorage.setItem(KEY, "dark");
      storageEvent(window, KEY, "dark");
      expect(bar(document)).toBe("rgb(10, 17, 32)");
    });

    it("keeps its colour while there is no stylesheet to read", async () => {
      const { document, picker } = await load(
        '<meta name="theme-color" content="#f6f7f9">' + PICKER,
      );
      choose(picker, "dark");
      expect(bar(document)).toBe("#f6f7f9");
    });
  });
});

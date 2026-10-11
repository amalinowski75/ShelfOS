// The colour theme: System, Light, Dark, Dim or Warm, picked in the Settings
// menu and remembered in this browser only — a phone by the shelves and a
// monitor on the desk are often wanted differently.
//
// Loaded in <head>, before the stylesheet, and synchronously on purpose: the
// theme has to be on <html> before the first paint, or a dark choice flashes
// white on every page load. The picker itself is wired once the body exists.
//
// "System" is no data-theme at all, and app.css then follows the OS.

(() => {
  const KEY = "shelfos-theme";
  const THEMES = ["light", "dark", "dim", "warm"];
  const root = document.documentElement;

  // Storage can throw (blocked site data, some private windows); the theme then
  // just follows the OS, and a choice made here lasts for this page only.
  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return THEMES.includes(value) ? value : "";
    } catch {
      return "";
    }
  }

  // A phone's browser bar (<meta name="theme-color">) takes the page's own
  // ground, read back from the stylesheet rather than kept as a second copy of
  // the palette. Before app.css has loaded there is nothing to read: the body's
  // background is still transparent, and the meta keeps what it has.
  function paintBrowserBar() {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta || !document.body) return;
    const ground = getComputedStyle(document.body).backgroundColor;
    if (!ground || ground === "transparent" || ground === "rgba(0, 0, 0, 0)") return;
    meta.content = ground;
  }

  function apply(theme) {
    if (theme) root.dataset.theme = theme;
    else delete root.dataset.theme;
    paintBrowserBar();
  }

  apply(stored());

  function wirePicker() {
    const picker = document.getElementById("theme-select");
    if (!picker) return; // signed out: no Settings menu
    picker.value = stored();
    picker.addEventListener("change", () => {
      const theme = THEMES.includes(picker.value) ? picker.value : "";
      apply(theme);
      try {
        if (theme) localStorage.setItem(KEY, theme);
        else localStorage.removeItem(KEY);
      } catch {
        // Applied to this page already; nothing more can be done.
      }
    });
  }

  // A choice made in another ShelfOS tab reaches this one without a reload,
  // the sign-in page included. The event only fires in the tabs that did NOT
  // make the change; a null key means the whole storage was cleared.
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY && event.key !== null) return;
    const theme = stored();
    apply(theme);
    const picker = document.getElementById("theme-select");
    if (picker) picker.value = theme;
  });

  // System follows the OS, so the bar must too when the OS switches.
  if (window.matchMedia) {
    window
      .matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", paintBrowserBar);
  }
  // The first moment there is a body, and again once every stylesheet is in.
  window.addEventListener("load", paintBrowserBar);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      wirePicker();
      paintBrowserBar();
    });
  } else {
    wirePicker();
    paintBrowserBar();
  }
})();

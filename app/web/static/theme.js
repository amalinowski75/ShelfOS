// The colour theme: System, Light, Dark or Dim, picked in the Settings menu and
// remembered in this browser only — a phone by the shelves and a monitor on the
// desk are often wanted differently.
//
// Loaded in <head>, before the stylesheet, and synchronously on purpose: the
// theme has to be on <html> before the first paint, or a dark choice flashes
// white on every page load. The picker itself is wired once the body exists.
//
// "System" is no data-theme at all, and app.css then follows the OS.

(() => {
  const KEY = "shelfos-theme";
  const THEMES = ["light", "dark", "dim"];
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

  function apply(theme) {
    if (theme) root.dataset.theme = theme;
    else delete root.dataset.theme;
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wirePicker);
  } else {
    wirePicker();
  }
})();

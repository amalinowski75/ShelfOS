// The top-bar Settings menu (base.html): one button on the right that opens the
// instance-level controls — the label printer, the accounts, the API docs and
// this account's password. Closes on a click anywhere else, on Escape, and after
// any item inside it is chosen, so it never sits open over the page.

const settingsBtn = document.getElementById("settings-btn");
if (settingsBtn) {
  const menu = document.getElementById("settings-menu");

  function setOpen(open) {
    menu.hidden = !open;
    settingsBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  settingsBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });

  // A link navigates and a button (Change password) opens its own dialog; either
  // way the menu has done its job.
  menu.addEventListener("click", () => setOpen(false));

  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target)) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      setOpen(false);
      settingsBtn.focus();
    }
  });
}

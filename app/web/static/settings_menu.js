// The top-bar Settings menu (base.html): one button on the right that opens the
// instance-level controls — the label printer, the accounts, the API docs and
// this account's password. Closes on a click anywhere outside it, on Escape, on
// the focus leaving it, and once an entry inside it is chosen.
//
// The entries are ordinary links and one button, deliberately: a `role="menu"`
// would promise arrow-key navigation and a roving tabindex that this does not
// implement, and it would take Tab away from a widget where Tab is what works.

const settingsBtn = document.getElementById("settings-btn");
if (settingsBtn) {
  const menu = document.getElementById("settings-menu");
  const settings = settingsBtn.closest(".settings");

  function setOpen(open) {
    menu.hidden = !open;
    settingsBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  settingsBtn.addEventListener("click", () => setOpen(menu.hidden));

  // An entry has been chosen: the link navigates, the button opens its dialog,
  // and either way the menu has done its job. Only an entry — the panel has
  // padding, and a click that lands short of one has chosen nothing.
  menu.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) setOpen(false);
  });

  // No stopPropagation on the button above: every click has to keep reaching
  // `document`, because other widgets (the location picker, the BOM menus) close
  // themselves from there and would be starved by a click that never arrives.
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !settings.contains(event.target)) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || menu.hidden) return;
    setOpen(false);
    // Only take the focus back if it is still ours to take. On the invoice page
    // a capture-phase handler blurs the page control on Escape to hand the
    // keyboard back to the barcode collector; parking the focus on the gear
    // afterwards would quietly disarm it again.
    if (settings.contains(document.activeElement)) settingsBtn.focus();
  });

  // Tab out of the last entry and the menu has been left behind; close it rather
  // than leave a panel open over the page with nothing in it focused.
  settings.addEventListener("focusout", (event) => {
    if (!menu.hidden && !settings.contains(event.relatedTarget)) setOpen(false);
  });
}

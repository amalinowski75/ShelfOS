// "About" in the Settings menu: the logo, what ShelfOS is, which build this is
// and where its source and changelog live. Everything in it is rendered by
// base.html; this only opens it. [data-close] buttons are wired in shared.js.

const aboutBtn = document.getElementById("about-btn");
if (aboutBtn) {
  const aboutDialog = document.getElementById("about-dialog");
  aboutBtn.addEventListener("click", () => aboutDialog.showModal());
}

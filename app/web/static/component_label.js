// The component detail page's Print label button (§7). Everything about the
// printer — the roll, the preview, the mismatch question, the running job —
// belongs to label_print.js; this only says which part is being labelled.
(() => {
  const button = document.getElementById("component-print-label-btn");
  if (!button) return; // no printer set up, or a reader who may not print

  const head = document.querySelector(".head h1");
  // The heading is the part number (or "Component #12" where there is none),
  // which is also the label's headline — so the dialog names what will come out
  // of the printer in the same words the page already used.
  const name = head ? head.textContent.trim() : "this component";
  const id = Number(button.dataset.componentId);

  button.addEventListener("click", () => {
    window.openLabelPrintDialog({
      kind: "components",
      ids: [id],
      preview: id,
      what: `One label: “${name}”`,
    });
  });
})();

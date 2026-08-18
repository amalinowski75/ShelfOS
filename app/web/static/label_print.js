// Printing location labels on the label printer (§7). The browser-printable
// page at /labels/locations is unchanged and still there; this is the direct
// path: pick a roll, see the bitmap the printer would get, send it.
//
// The one thing this screen has to get right is disagreement. The printer can
// say which tape it holds but not what anyone intends to do about it, so a job
// asking for a different roll comes back refused, with both tapes, and the
// question is put here rather than guessed at.
(() => {
  const dialog = document.getElementById("label-print-dialog");
  if (!dialog) return; // page does not include the dialog

  const form = document.getElementById("label-print-form");
  const whatEl = document.getElementById("label-print-what");
  const tapeEl = document.getElementById("label-print-tape");
  const hintEl = document.getElementById("label-print-tape-hint");
  const previewEl = document.getElementById("label-print-preview");
  const errorEl = document.getElementById("label-print-error");
  const mismatchEl = document.getElementById("label-print-mismatch");
  const mismatchText = document.getElementById("label-print-mismatch-text");
  const useLoadedBtn = document.getElementById("label-print-use-loaded");
  const recheckBtn = document.getElementById("label-print-recheck");
  const submitBtn = document.getElementById("label-print-submit");

  let target = null; // {ids | root, what, preview}
  let loaded = null; // the tape the printer last said it holds
  let printing = false;

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  function clearMessages() {
    errorEl.hidden = true;
    mismatchEl.hidden = true;
  }

  function refreshPreview() {
    if (!target || !target.preview) return;
    // Cache-busted per selection: same URL, different tape, and the browser
    // would otherwise keep showing the first one.
    previewEl.src =
      `/api/labels/locations/${target.preview}/preview.png` +
      `?tape=${encodeURIComponent(tapeEl.value)}`;
  }

  function describeTape(id) {
    const option = [...tapeEl.options].find((o) => o.value === id);
    return option ? option.textContent.trim() : id;
  }

  async function loadTapes() {
    tapeEl.innerHTML = "";
    hintEl.textContent = "";
    let data;
    try {
      const resp = await fetch("/api/labels/tapes");
      if (!resp.ok) {
        showError(await errorMessage(resp, "Could not read the tape list."));
        return;
      }
      data = await resp.json();
    } catch {
      showError("Could not reach the server. Please try again.");
      return;
    }
    for (const tape of data.tapes) {
      const option = document.createElement("option");
      option.value = tape.id;
      option.textContent = tape.name;
      tapeEl.appendChild(option);
    }
    loaded = data.loaded;
    // Pre-select what is actually in the machine, so the common case — print on
    // whatever is loaded — needs no decision at all.
    tapeEl.value = loaded || data.configured;
    hintEl.textContent = loaded
      ? `The printer is holding ${describeTape(loaded)}.`
      : "The printer is not saying what it holds; pick the roll you loaded.";
    refreshPreview();
  }

  async function send(acceptLoaded) {
    if (printing) return;
    printing = true;
    submitBtn.disabled = true;
    clearMessages();
    try {
      const body = { tape: tapeEl.value, accept_loaded: acceptLoaded };
      if (target.root != null) body.root = target.root;
      if (target.ids != null) body.ids = target.ids;

      let resp;
      try {
        resp = await fetch("/api/labels/locations/print", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify(body),
        });
      } catch {
        showError("Could not reach the server. Please try again.");
        return;
      }
      if (resp.status === 409) {
        const conflict = await resp.json().catch(() => ({}));
        if (conflict.loaded) {
          loaded = conflict.loaded;
          mismatchText.textContent =
            `The printer is holding ${describeTape(conflict.loaded)}, ` +
            `but you asked for ${describeTape(conflict.requested)}.`;
          mismatchEl.hidden = false;
          return;
        }
      }
      if (!resp.ok) {
        showError(await errorMessage(resp, "The labels could not be printed."));
        return;
      }
      const result = await resp.json();
      const count = `${result.sent} label${result.sent === 1 ? "" : "s"}`;
      // "Printed" only when the printer said so: a job can be accepted and then
      // sit there because the cover is open, and the write cannot tell.
      showToast(
        result.confirmed
          ? `Printed ${count} on ${describeTape(result.tape)}.`
          : `Sent ${count} to the printer.`,
        { tone: result.confirmed ? "ok" : "warn" },
      );
      dialog.close();
    } finally {
      printing = false;
      submitBtn.disabled = false;
    }
  }

  tapeEl.addEventListener("change", () => {
    clearMessages();
    refreshPreview();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    send(false);
  });

  useLoadedBtn.addEventListener("click", () => {
    tapeEl.value = loaded || tapeEl.value;
    refreshPreview();
    send(true);
  });

  recheckBtn.addEventListener("click", async () => {
    // The roll changed under the printer, so everything it told us is stale —
    // including which tape to pre-select.
    const chosen = tapeEl.value;
    await loadTapes();
    tapeEl.value = chosen;
    refreshPreview();
    send(false);
  });

  window.openLabelPrintDialog = (options) => {
    target = options;
    whatEl.textContent = options.what || "";
    clearMessages();
    previewEl.removeAttribute("src");
    dialog.showModal();
    loadTapes();
  };
})();

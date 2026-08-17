// Scan putaway: scan a thing → its "Set location" dialog opens; scan an SL<id>
// location label → the location is saved, the dialog closes, ready for the next
// one. Shared by the draft invoice (file a bag against its line) and the
// components page (relocate a bag's stock); each page supplies an ADAPTER
// saying what a scanned code resolves to and what saving means, via
// window.initScanPutaway(). Markup comes from templates/_putaway.html; helpers
// (csrfToken, errorMessage, showToast) from shared.js.
//
// The wedge scanner is a keyboard, but this deliberately does NOT type into a
// focused input. Keystrokes are collected in a document-level capture-phase
// buffer and the (readonly) fields only DISPLAY it. Field tested: password
// managers (Keeper) pounce on a programmatically focused input and shove their
// own overlay iframe in front of it — focus leaves the document entirely
// (document.hasFocus() === false with the ring still painted) and every
// keystroke vanishes into the extension. No focused input, no overlay, no
// stolen scans.
//
// Two rules follow from field testing, and both are about NOT guessing:
//
//   * Nothing here reads intent from typing speed. This scanner emits its
//     payload in packets with 100 ms+ stalls, so a "that gap means a new
//     series" rule truncated real codes; and after the first character a
//     human typing quickly is indistinguishable from a payload anyway.
//   * Who owns the keyboard is read from FOCUS instead: while a page control
//     (button, select, link) holds focus the collector is silent and the user
//     keeps type-ahead, Space and Enter; otherwise every key is a scan's. The
//     armed ring shows which state is in force, and Escape hands the keyboard
//     back to scanning without touching the mouse.

// Give up on a hung request rather than holding the flow busy forever — a stuck
// guard would silently eat every scan that follows.
const SCAN_FETCH_TIMEOUT_MS = 10_000;

// The adapters' one way to talk to the server: CSRF, JSON and the timeout in
// one place, so no adapter can forget any of them.
async function scanFetch(url, method, body) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(SCAN_FETCH_TIMEOUT_MS),
  });
}

// A miss the user must be told about ("no line matches…"), as opposed to a
// crash. Adapters throw it from resolve()/save(); the message is shown as-is.
class ScanMiss extends Error {}

window.initScanPutaway = function (adapter) {
  const panel = document.getElementById("scan-panel");
  if (!panel) return; // read-only viewer, or a page state with nothing to file

  const scanInput = document.getElementById("scan-input");
  const statusEl = document.getElementById("scan-status");
  const dialog = document.getElementById("putaway-dialog");
  const form = document.getElementById("putaway-form");
  const partEl = document.getElementById("putaway-part");
  const descEl = document.getElementById("putaway-desc");
  const locationInput = document.getElementById("putaway-scan");
  const locationSelect = document.getElementById("putaway-select");
  const qtyInput = document.getElementById("putaway-qty");
  const qtyHint = document.getElementById("putaway-qty-hint");
  const errorEl = document.getElementById("putaway-error");

  // id → human path, for showing where the part just went. The server
  // re-validates the id on save, so this map is presentation, not authority.
  const LOCATIONS = new Map(
    JSON.parse(panel.dataset.locations || "[]").map((o) => [String(o.id), o.path]),
  );
  // What our own location labels encode (see app/services/label_service.py).
  const SL = /^SL(\d+)$/i;

  let target = null; // what the last resolved scan is waiting to file
  let busy = false;
  let buffer = ""; // keystrokes collected since the last Enter
  let queued = null; // a scan that arrived while the previous one was busy
  let idleTimer = null;

  // The ONE timing rule left, and it can only ever discard: a buffer nobody
  // terminated is dropped after this much silence, so an abandoned half-scan
  // cannot prefix the next one. Deliberately far longer than any gap inside a
  // payload — this scanner emits characters in packets with 100 ms+ stalls
  // between them, and every threshold that tried to read intent from key
  // spacing ended up truncating a real code.
  const IDLE_RESET_MS = 1200;

  const HINT_DISARMED =
    "Keyboard is with the page — press Escape (or click a blank spot) to scan.";
  const HINT_WINDOW_BLUR =
    "This window is not focused — scans are going elsewhere. " +
    "Click the page (or alt-tab back) to resume.";

  // A control whose own keyboard behaviour belongs to the user: while one of
  // these holds focus the collector stands down completely, so type-ahead,
  // Space-activates-button and Enter-submits all work untouched — and nothing
  // typed there can leak into a scan. Inside the putaway dialog nothing
  // qualifies: there the keyboard exists to scan (its buttons stay mouse- and
  // Escape-operated, its select mouse- and arrow-operated), and a payload's
  // stray Space must never "click" the close button showModal focused.
  function isPageControl(node) {
    if (!node || dialog.contains(node)) return false;
    return ["BUTTON", "SELECT", "A", "SUMMARY"].includes(node.tagName);
  }

  // Everything the collector must keep its hands off: page controls, text
  // entry anywhere (including the dialog's own quantity box), and another
  // dialog's contents. One predicate for both the keydown guard and the armed
  // ring, so the ring can never claim a scan would be collected when it
  // wouldn't.
  function ownsKeyboard(node) {
    if (!node) return true;
    if (isPageControl(node)) return false;
    if (node.isContentEditable || node.tagName === "TEXTAREA") return false;
    if (node.tagName === "INPUT" && node !== scanInput && node !== locationInput)
      return false;
    const openDialog = node.closest?.("dialog[open]");
    return !(openDialog && openDialog !== dialog);
  }

  const armed = () => ownsKeyboard(document.activeElement);

  // The live field displays the buffer; the idle one sits empty. Both are
  // readonly — they are gauges, not text entry. The ring marks which field a
  // scan would land in, and goes out when the collector has stood down, so
  // "the keyboard isn't mine right now" is never invisible.
  function render() {
    const live = dialog.open ? locationInput : scanInput;
    const idle = dialog.open ? scanInput : locationInput;
    live.value = buffer;
    idle.value = "";
    live.classList.toggle("scan-armed", armed());
    idle.classList.remove("scan-armed");
  }

  // Both message slots keep their real content in a variable and repaint from
  // it. The transient hints below borrow the slots, and an alt-tab round trip
  // must never erase a genuine error the user has not acted on yet.
  let statusText = "";
  let statusTone = "";
  let dialogError = "";
  let windowUnfocused = false;

  // Text only — never `hidden`, never a height change. This panel shares the
  // page with a table sized to fit it (shared.js frameTable), so a panel that
  // grows or shrinks re-lays out that table, and a row button rebuilt between
  // mousedown and mouseup eats the click that was already on its way. The line
  // is reserved and clipped in CSS (.scan-status); the full text of anything
  // long also goes out as a toast, and rides here as a tooltip.
  function paintStatus(message, tone) {
    statusEl.textContent = message;
    statusEl.className = `scan-status ${tone === "error" ? "error" : "muted"}`;
    statusEl.title = message;
  }

  function refreshStatus() {
    // The disarmed hint is skipped while the dialog is up: the panel sits
    // behind the modal, so it would be advice nobody can read. Inside the
    // dialog only the quantity box takes the keyboard, and it hands it back on
    // the first letter of a scan.
    if (windowUnfocused) paintStatus(HINT_WINDOW_BLUR, "error");
    else if (!armed() && !dialog.open) paintStatus(HINT_DISARMED, "muted");
    else paintStatus(statusText, statusTone);
  }

  function setStatus(message, tone) {
    statusText = message;
    statusTone = tone;
    refreshStatus();
  }

  function paintDialogError(message) {
    errorEl.textContent = message;
    errorEl.hidden = !message;
  }

  function setDialogError(message) {
    dialogError = message;
    paintDialogError(windowUnfocused ? HINT_WINDOW_BLUR : message);
  }

  // An error the user must not miss mid-scanning: the small status line plus a
  // toast (eyes are on the bags, not on the panel).
  function scanError(message) {
    setStatus(message, "error");
    showToast(message, { tone: "warn" });
  }

  // Drop a buffer that never got its terminator (a stray keypress, an aborted
  // scan), so it cannot prefix the next code. Rearmed on every keystroke, so
  // it can only fire between scans, never inside one.
  function armIdleReset() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (!buffer) return;
      buffer = "";
      render();
    }, IDLE_RESET_MS);
  }

  function drainQueue() {
    if (queued && !busy && !dialog.open) {
      const code = queued;
      queued = null;
      handleThingScan(code);
    }
  }

  async function handleThingScan(code) {
    if (!code) return;
    if (busy || dialog.open) {
      // NEVER drop a scan silently — hold the latest one and run it as soon
      // as the current work (a lookup, or a dialog mid-close) is done.
      queued = code;
      setStatus("One moment — finishing the previous scan…");
      return;
    }
    if (SL.test(code)) {
      scanError("That is a location label — scan the item first.");
      return;
    }
    busy = true;
    setStatus("Reading…");
    try {
      const resolved = await adapter.resolve(code);
      setStatus("");
      // A falsy result means the adapter took the scan somewhere else itself
      // (e.g. the components page opens the New Component dialog for a code that
      // matches nothing yet) — so there is nothing to file here, and no miss to
      // report. resolve() otherwise returns a target or throws ScanMiss.
      if (!resolved) return;
      target = resolved;
      partEl.textContent = resolved.label;
      descEl.textContent = resolved.description || "";
      descEl.hidden = !descEl.textContent;
      locationSelect.value =
        resolved.locationId == null ? "" : String(resolved.locationId);
      qtyInput.value = resolved.quantity == null ? "" : String(resolved.quantity);
      qtyInput.max = resolved.maxQuantity == null ? "" : String(resolved.maxQuantity);
      qtyHint.textContent = resolved.quantityHint || "";
      setDialogError("");
      buffer = "";
      dialog.showModal();
      render(); // no focus() — see the header comment
    } catch (error) {
      scanError(
        error instanceof ScanMiss ? error.message : "Could not reach the server.",
      );
    } finally {
      busy = false;
      drainQueue();
    }
  }

  function handleLocationScan(code) {
    if (busy) {
      // Same contract as item scans: a scan is never dropped in silence. It is
      // not queued, because by the time the in-flight save lands this dialog
      // may be gone and the code would apply to the wrong part.
      setDialogError("Still saving — scan the location label again in a moment.");
      return;
    }
    const matched = SL.exec(code);
    if (!matched) {
      setDialogError("That is not a location label — scan the SL… code on the shelf.");
      return;
    }
    saveLocation(Number(matched[1]));
  }

  // The quantity to file, or null after explaining what's wrong with the box.
  // An adapter that doesn't use quantities (no prefill) gets undefined back and
  // never sees the field.
  function readQuantity(saving) {
    if (saving.quantity == null) return undefined;
    const value = Number(qtyInput.value);
    if (!Number.isInteger(value) || value < 1) {
      setDialogError("Quantity must be a whole number, 1 or more.");
      return null;
    }
    if (saving.maxQuantity != null && value > saving.maxQuantity) {
      setDialogError(`Only ${saving.maxQuantity} available — cannot file ${value}.`);
      return null;
    }
    return value;
  }

  async function saveLocation(locationId) {
    if (busy || !target) return;
    // Pin the target for the whole round trip: the dialog's own close listener
    // clears it, so a Cancel/Escape landing while the save is in flight would
    // otherwise turn a successful save into a phantom network error and leave
    // the page showing a location the server has already recorded.
    const saving = target;
    const path = LOCATIONS.get(String(locationId));
    if (!path) {
      setDialogError(`Unknown location code SL${locationId} — reprint the label?`);
      return;
    }
    const quantity = readQuantity(saving);
    if (quantity === null) return; // readQuantity explained why
    busy = true;
    try {
      await saving.save(locationId, path, quantity);
      if (target === saving) target = null;
      dialog.close(); // a no-op if the user already closed it
      showToast(`${saving.label} → ${path}`, { tone: "ok" });
    } catch (error) {
      setDialogError(
        error instanceof ScanMiss ? error.message : "Could not reach the server.",
      );
    } finally {
      busy = false;
      drainQueue();
    }
  }

  // The collector. Capture phase on the document, so it sees every keystroke
  // before any control can swallow it, wherever focus happens to rest — the
  // whole point of the design (see the header comment).
  //
  // WHO the keyboard belongs to is read from focus, never guessed from typing
  // speed. After the first character a human typing quickly at a control and a
  // scanner payload are indistinguishable, and this scanner's payloads stall
  // for 100 ms+ between packets, so every speed threshold either truncated a
  // real code or ate a real keystroke. So: a page control (or another dialog,
  // a text area, a foreign input) holds focus → the collector is silent and
  // the user has their keyboard, whole. Otherwise every key is consumed and
  // appended, and only an unterminated buffer expires (armIdleReset).
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.ctrlKey || event.altKey || event.metaKey) return;
      const t = event.target instanceof HTMLElement ? event.target : null;
      if (t && !ownsKeyboard(t)) {
        if (t === qtyInput) {
          // A scan already in progress (buffer non-empty) keeps its keystrokes
          // even if the box somehow still has focus — otherwise the digits of
          // "SL9" would go back to the count once the letters had escaped.
          if (!buffer) {
            if (event.key === "Enter") {
              // Enter finishes editing the count: it must not submit the form
              // (the location isn't scanned yet), and the collector needs the
              // keyboard back so the next scan isn't typed into this box.
              event.preventDefault();
              qtyInput.blur();
              return;
            }
            if (event.key === "Escape") {
              // "Keep the dialog, give me the scanner back" — the dialog's own
              // Escape-closes is suppressed; a second Escape closes it.
              event.preventDefault();
              qtyInput.blur();
              reflectFocus();
              return;
            }
            // Digits, Backspace and the arrows are the box's own business.
            if (event.key.length !== 1 || /[0-9]/.test(event.key)) return;
            // A LETTER cannot be part of a count, so it can only be a scan
            // starting while this box still holds focus — the natural sequence
            // (click the box, type the real count, scan the shelf). Hand the
            // keyboard back; the fall-through below opens the buffer with it,
            // instead of the number input silently dropping the "SL" and
            // appending the digits to the count.
            qtyInput.blur();
          }
        } else {
          // The user is working the UI (a page control, a foreign input):
          // type-ahead, Space and Enter all reach it untouched, and nothing
          // typed there can join a scan. One keyboard-only way back to
          // scanning, since nothing else here moves focus: Escape.
          if (event.key === "Escape") {
            t.blur();
            reflectFocus();
          }
          return;
        }
      }
      if (event.key === "Enter") {
        if (!buffer) {
          // A scanner's extra terminator (CR+LF, double CR) lands right after
          // the code's own Enter; with the dialog just opened and its close
          // button focused, letting it through would "click" that button and
          // the dialog would vanish unseen. Elsewhere plain Enter still
          // reaches forms and buttons.
          if (dialog.open) event.preventDefault();
          return;
        }
        event.preventDefault();
        clearTimeout(idleTimer);
        const code = buffer.trim();
        buffer = "";
        render();
        if (dialog.open) handleLocationScan(code);
        else handleThingScan(code);
        return;
      }
      if (event.key === "Backspace" && buffer) {
        event.preventDefault();
        buffer = buffer.slice(0, -1);
        render();
        armIdleReset();
        return;
      }
      if (event.key.length !== 1) return; // printable characters only
      event.preventDefault();
      buffer += event.key;
      render();
      armIdleReset();
    },
    true,
  );

  // Focus moving in or out of a page control flips the collector, so the ring
  // and the hint track it without any polling.
  function reflectFocus() {
    render();
    refreshStatus();
  }
  document.addEventListener("focusin", reflectFocus);
  // focusout fires BEFORE the next element takes focus, so read the result a
  // tick later or every blur would look like "nothing is focused".
  document.addEventListener("focusout", () => setTimeout(reflectFocus, 0));

  // A scan can't reach this page while ANOTHER OS window holds focus — which
  // looks exactly like a dead field, because the browser keeps the focus ring
  // painted in an unfocused window. Say so loudly instead of staying silent.
  // It only BORROWS the message slot: the genuine message is repainted on
  // return, so an alt-tab round trip can't erase an error the user has not
  // acted on yet ("Unknown location code SL777 — reprint the label?").
  window.addEventListener("blur", () => {
    windowUnfocused = true;
    if (dialog.open) paintDialogError(HINT_WINDOW_BLUR);
    else refreshStatus();
  });
  window.addEventListener("focus", () => {
    if (!windowUnfocused) return;
    windowUnfocused = false;
    paintDialogError(dialogError);
    refreshStatus();
  });

  // Manual fallback for a torn or missing label: pick from the select (by
  // mouse) and Save.
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!locationSelect.value) {
      setDialogError("Scan a location label or pick one from the list.");
      return;
    }
    saveLocation(Number(locationSelect.value));
  });

  // However the dialog closes (save, Cancel, Escape), the collector switches
  // back to the item field — state, not focus, decides where scans go — and an
  // item scanned a beat too early gets its turn.
  dialog.addEventListener("close", () => {
    target = null;
    buffer = "";
    render();
    drainQueue();
  });

  render(); // arm the item field from the start
};

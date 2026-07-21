// Helpers shared by every page's script (component table + invoice workflow).

// Echoed back on cookie-authenticated writes so the server can tell a real
// same-origin request from a forged cross-site one (see require_csrf).
const csrfToken =
  document.querySelector('meta[name="csrf-token"]')?.content || "";

// The signed-in user's role, and whether they may modify state. Read-only
// accounts are GET-only on the server, so client-rendered write affordances
// (e.g. the component table's Add/Take buttons) are hidden for them rather than
// shown and rejected on submit. This is UX only — the server (require_access)
// remains the actual boundary.
//
// An empty role (no meta) is treated as non-writer. That is safe because these
// scripts only load on pages rendered with a current_user (base.html gates them
// behind `{% if current_user %}`); if that ever changes, a writer on a
// current_user-less page would wrongly lose write buttons.
const userRole =
  document.querySelector('meta[name="user-role"]')?.content || "";
const canWrite = userRole !== "" && userRole !== "read-only";

// Per-page cache of attachment-list GETs, so the attachments panel and the image
// gallery (both on the component-detail page) don't each fetch the same feed.
// Returns a promise of the parsed rows; pass {fresh:true} after a write to skip
// the cache and refetch. A failed fetch is not cached, so the next call retries.
const _attachmentFeeds = new Map();
function fetchAttachmentList(url, { fresh = false } = {}) {
  if (fresh) _attachmentFeeds.delete(url);
  if (!_attachmentFeeds.has(url)) {
    const pending = fetch(url).then((resp) => {
      if (!resp.ok) throw new Error("attachments feed failed");
      return resp.json();
    });
    pending.catch(() => _attachmentFeeds.delete(url));
    _attachmentFeeds.set(url, pending);
  }
  return _attachmentFeeds.get(url);
}

// HTML-escape a value for safe interpolation into innerHTML.
function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

// Frame a Tabulator table: fill it from its top down to the bottom of the viewport
// (a sticky header + internal scroll), wrapping shorter tables exactly so there's
// no empty frame. Uses a FIXED pixel height — never `height`/`maxHeight` set to a
// `vh`/`%` value, which make Tabulator recompute its height on every resize event
// and hit an internal recursion that freezes the UI for tens of seconds. The
// height is recomputed on a DEBOUNCED window resize (a one-shot px value, not the
// continuous relative recalculation), so the table tracks the window safely.
// Call after each `setData`; the resize listener is attached only once per table.
function frameTable(table) {
  const fit = () => {
    const el = table.element;
    if (!el || el.offsetParent === null) return; // gone or hidden → scrollHeight is 0
    const holder = el.querySelector(".tabulator-tableholder");
    if (!holder) return;
    const header = el.querySelector(".tabulator-header");
    const headerH = header ? header.offsetHeight : 0;
    // scrollHeight reflects the FULL content (all rows) even when a height is
    // already applied, so this measurement stays stable across re-fits. The +16
    // leaves room for a horizontal scrollbar so a short-but-wide table doesn't get
    // a spurious vertical one.
    const full = holder.scrollHeight + headerH + 16;
    // Grow/shrink the table so the WHOLE page's bottom lands just above the viewport
    // bottom, so the page itself never scrolls. Measuring the live page bottom
    // accounts for everything above AND below the table (nav, headings, and any
    // wrapping card's + the page's own bottom padding) without hard-coding any of
    // it — the BOM table sits in a padded card, the components table doesn't.
    // Resolves in one pass: a change in the table's height shifts the page bottom by
    // the same amount.
    const pageEl = el.closest(".page") || document.body;
    const pageBottom = pageEl.getBoundingClientRect().bottom;
    const curH = el.getBoundingClientRect().height;
    const avail = curH + window.innerHeight - pageBottom - 8;
    // Cap at the content height (short tables wrap exactly) and floor at header +
    // ~one row so a tiny viewport can't size the table below its own header.
    table.setHeight(Math.round(Math.max(headerH + 40, Math.min(avail, full))));
  };
  if (!table._framed) {
    table._framed = true;
    let timer;
    window.addEventListener("resize", () => {
      clearTimeout(timer);
      timer = setTimeout(fit, 150);
    });
  }
  fit();
}

// A readable message from an already-parsed API error body. Tolerates FastAPI's
// list-shaped 422 `detail` so the user never sees "[object Object]".
function errorTextFromBody(body, fallback = "Request failed") {
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const joined = detail
      .map((item) => item?.msg)
      .filter(Boolean)
      .join("; ");
    if (joined) return joined;
  }
  return fallback;
}

// A readable message from a failed JSON API response. Tolerates a non-JSON body
// (proxy/500 HTML, network error). Use when only the text is needed; when the
// caller also needs a structured field (e.g. existing_id), read the body itself
// and call errorTextFromBody.
async function errorMessage(resp, fallback = "Request failed") {
  let body;
  try {
    body = await resp.json();
  } catch {
    return fallback;
  }
  return errorTextFromBody(body, fallback);
}

// Every dialog closes via a [data-close] button; wire them once for the page.
document
  .querySelectorAll("[data-close]")
  .forEach((btn) =>
    btn.addEventListener("click", () => btn.closest("dialog")?.close()),
  );

// A short-lived, non-blocking notice. Used when a background step fails after the
// user's dialog has already closed — e.g. the component was created but its
// datasheet couldn't be downloaded — where an alert() would be an interruption and
// silence would be a lie. Stacks if several fire; click to dismiss early.
function showToast(message, { tone = "warn", timeout = 5000 } = {}) {
  let tray = document.querySelector(".toast-tray");
  if (!tray) {
    tray = document.createElement("div");
    tray.className = "toast-tray";
    document.body.appendChild(tray);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${tone}`;
  toast.setAttribute("role", "status");
  toast.textContent = message; // textContent, never innerHTML: callers pass shop text
  const dismiss = () => toast.remove();
  toast.addEventListener("click", dismiss);
  tray.appendChild(toast);
  if (timeout) setTimeout(dismiss, timeout);
  return toast;
}

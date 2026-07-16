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

// Frame a Tabulator table: a sticky header + internal scroll once its content is
// taller than ~`fraction` of the viewport, wrapping shorter tables exactly (no
// empty frame). Uses a FIXED pixel height — never `height`/`maxHeight` set to a
// `vh`/`%` value, which make Tabulator recompute its height on every resize event
// and hit an internal recursion that freezes the UI for tens of seconds. The
// height is recomputed on a DEBOUNCED window resize (a one-shot px value, not the
// continuous relative recalculation), so the table tracks the window safely.
// Call after each `setData`; the resize listener is attached only once per table.
function frameTable(table, fraction = 0.7) {
  const fit = () => {
    const el = table.element;
    if (!el) return;
    const holder = el.querySelector(".tabulator-tableholder");
    if (!holder) return;
    const header = el.querySelector(".tabulator-header");
    const headerH = header ? header.offsetHeight : 0;
    // scrollHeight reflects the FULL content (all rows) even when a height is
    // already applied, so this measurement stays stable across re-fits. The +16
    // leaves room for a horizontal scrollbar so a short-but-wide table doesn't get
    // a spurious vertical one.
    const full = holder.scrollHeight + headerH + 16;
    const cap = Math.round(window.innerHeight * fraction);
    table.setHeight(Math.min(cap, full));
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

// A readable message from a failed JSON API response. Tolerates a non-JSON body
// (proxy/500 HTML, network error) and FastAPI's list-shaped 422 `detail` so the
// user never sees an unhandled rejection or "[object Object]".
async function errorMessage(resp, fallback = "Request failed") {
  let body;
  try {
    body = await resp.json();
  } catch {
    return fallback;
  }
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

// Every dialog closes via a [data-close] button; wire them once for the page.
document
  .querySelectorAll("[data-close]")
  .forEach((btn) =>
    btn.addEventListener("click", () => btn.closest("dialog")?.close()),
  );

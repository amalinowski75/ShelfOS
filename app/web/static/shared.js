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

// HTML-escape a value for safe interpolation into innerHTML.
function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
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

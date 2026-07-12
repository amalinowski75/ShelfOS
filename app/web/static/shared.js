// Helpers shared by every page's script (component table + invoice workflow).

// Echoed back on cookie-authenticated writes so the server can tell a real
// same-origin request from a forged cross-site one (see require_csrf).
const csrfToken =
  document.querySelector('meta[name="csrf-token"]')?.content || "";

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

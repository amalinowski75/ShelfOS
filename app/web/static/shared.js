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

// Our own location labels encode the shelf id as "SL<id>" (defined by
// label_service.py). This is the ONE client-side copy of that format, shared by
// the two scanners that read it — the putaway panel and the stock dialog — so the
// format lives in as few places as the server's. Returns the numeric id, or null
// when the text isn't one of our shelf labels.
function shelfLabelId(code) {
  const matched = /^SL(\d+)$/i.exec(String(code ?? "").trim());
  return matched ? Number(matched[1]) : null;
}

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
// A toast that survives the reload the caller is about to do. Several flows
// save something and then reload the page to show it; anything they have to
// report about a SECOND step — "created, but the label did not print" — would
// otherwise be wiped off the screen a moment after appearing.
const PENDING_TOAST_KEY = "shelfos:pending-toast";

function showToastAfterReload(message, options) {
  try {
    sessionStorage.setItem(
      PENDING_TOAST_KEY,
      JSON.stringify({ message, options: options || {} }),
    );
  } catch {
    showToast(message, options); // private mode, or storage full: say it now
  }
}

function drainPendingToast() {
  let stored;
  try {
    stored = sessionStorage.getItem(PENDING_TOAST_KEY);
    sessionStorage.removeItem(PENDING_TOAST_KEY);
  } catch {
    return;
  }
  if (!stored) return;
  try {
    const { message, options } = JSON.parse(stored);
    if (message) showToast(message, options);
  } catch {
    /* a mangled entry is not worth a broken page */
  }
}

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

drainPendingToast();

// --- match-rule aliases (match_rules.js, match_rule_dialog.js, param_matchers.js) ---
//
// The engine still holds ONE RULE PER ALIAS — that is what keeps the duplicate guard,
// the sort order and the audit trail per alias. These helpers only change how a
// target's aliases are written and read: as one comma-separated list ("biały, czarny,
// różowy" → "Kolor"), so a target's whole vocabulary sits in one field instead of one
// row each. Adding a separate rule for a target that already has some is still fine —
// it simply joins the list it belongs to.
const ALIAS_SEPARATOR = ", ";

// "biały, czarny ,, biały" -> ["biały", "czarny"]. A repeat within one list is kept
// once: the server rejects the second copy as a duplicate, which would be a confusing
// error to raise against text the user wrote in a single breath. Folded with
// toLowerCase, which is how the engine keys a global domain's aliases — a scoped
// domain folds harder still (accents, punctuation), so its true duplicates are caught
// by the server rather than here.
function splitAliases(text) {
  const seen = new Map();
  for (const part of String(text ?? "").split(",")) {
    const alias = part.trim();
    if (alias && !seen.has(alias.toLowerCase())) seen.set(alias.toLowerCase(), alias);
  }
  return [...seen.values()];
}

// Group a flat rule feed into one row per target — same domain, same target, same
// scope. The row carries its own `rules` so an edit knows which ids to write to, and
// `alias` is the joined list the field shows.
//
// `sort_order` is the group's LOWEST: the engine takes the first matching rule in sort
// order, so the lowest is the one that actually decides the group's precedence.
function groupRulesByTarget(rules) {
  const groups = new Map();
  for (const rule of rules || []) {
    const key = JSON.stringify([
      rule.domain,
      rule.canonical,
      rule.parameter_definition_id ?? null,
    ]);
    const group = groups.get(key);
    if (group) {
      group.rules.push(rule);
      group.sort_order = Math.min(group.sort_order, rule.sort_order);
    } else {
      // The group's id is its first rule's — unique across groups, which is what
      // Tabulator indexes rows by.
      groups.set(key, { ...rule, rules: [rule] });
    }
  }
  return [...groups.values()].map((group) => ({
    ...group,
    alias: group.rules.map((r) => r.alias).join(ALIAS_SEPARATOR),
  }));
}

// The writes that make a group's rules match an edited alias list, or null if the list
// is empty (removing a target is what the Delete button is for — silently deleting
// every rule because a field was cleared is not an edit anyone asked for).
//
// A rename is PATCHed in place rather than deleted and recreated: it keeps the audit
// entry as "alias changed" instead of a delete plus an unrelated create, and it is the
// only shape that survives a case-only fix, where a create would race the duplicate
// guard against the very rule being replaced.
function aliasListWrites(group, text) {
  const wanted = splitAliases(text);
  if (!wanted.length) return null;
  const folded = (value) => value.toLowerCase();
  const keep = new Set(wanted.map(folded));
  const held = new Set(group.rules.map((r) => folded(r.alias)));
  const stale = group.rules.filter((r) => !keep.has(folded(r.alias)));
  const fresh = wanted.filter((alias) => !held.has(folded(alias)));
  const writes = [];
  const renames = Math.min(stale.length, fresh.length);
  for (let i = 0; i < renames; i += 1) {
    writes.push({ method: "PATCH", id: stale[i].id, body: { alias: fresh[i] } });
  }
  for (const rule of stale.slice(renames)) {
    writes.push({ method: "DELETE", id: rule.id });
  }
  for (const alias of fresh.slice(renames)) {
    writes.push({
      method: "POST",
      body: {
        domain: group.domain,
        alias,
        canonical: group.canonical,
        parameter_definition_id: group.parameter_definition_id ?? null,
        sort_order: group.sort_order,
      },
    });
  }
  return writes;
}

// Every rule write goes through here: admin + CSRF, same as the endpoints demand.
async function sendMatchRuleWrite(url, method, payload) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}

// Run a group's writes in order, stopping at the first refusal and returning its
// message (null when they all land). Several requests stand behind one edit, so a
// caller must reload rather than trust the text that was typed — a stop halfway
// through leaves the earlier writes done, and the list has to show that.
async function runMatchRuleWrites(writes) {
  for (const write of writes) {
    const url = write.id
      ? `/api/admin/match-rules/${write.id}`
      : "/api/admin/match-rules";
    let resp;
    try {
      resp = await sendMatchRuleWrite(url, write.method, write.body);
    } catch {
      return "Could not reach the server.";
    }
    if (!resp.ok) return await errorMessage(resp);
  }
  return null;
}

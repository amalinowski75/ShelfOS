// The audit log (admin only, spec §19). A Tabulator over /web/api/audit, which
// hands back rows already in words — the log's own field names are terse and
// parameterised ("quantity@location:5"), and turning those into English is the
// server's job, next to the parsers that built them.
//
// Read-only by design: an audit trail that can be edited from the app it audits
// is not one. `esc` comes from shared.js.
(() => {
  const mount = document.getElementById("audit-table");
  if (!mount) return;

  const entitySelect = document.getElementById("audit-entity");
  const moreBtn = document.getElementById("audit-more");
  const countEl = document.getElementById("audit-count");
  const PAGE = 200;

  let loaded = [];
  let more = false;

  function change(cell) {
    const row = cell.getRow().getData();
    // An absent value is a real state — a field that had nothing, or was
    // cleared — so it is shown as such rather than left blank and ambiguous.
    const dash = '<span class="muted">—</span>';
    const from =
      row.old === null
        ? dash
        : `<span class="cell-mono">${esc(row.old)}</span>`;
    const to =
      row.new === null
        ? dash
        : `<span class="cell-mono">${esc(row.new)}</span>`;
    return `${from} <span class="muted">→</span> ${to}`;
  }

  const table = new Tabulator(mount, {
    layout: "fitDataFill",
    placeholder: "Nothing recorded yet",
    columns: [
      {
        title: "When",
        field: "when",
        width: 170,
        formatter: (cell) =>
          `<span class="cell-mono">${esc(cell.getValue().replace("T", " "))}</span>`,
      },
      { title: "Who", field: "who", width: 130 },
      {
        title: "What",
        field: "entity",
        width: 220,
        formatter: (cell) => {
          const row = cell.getRow().getData();
          const label = esc(cell.getValue());
          return row.entity_url
            ? `<a href="${esc(row.entity_url)}">${label}</a>`
            : label;
        },
      },
      { title: "Field", field: "what", width: 220 },
      {
        title: "Change",
        field: "change",
        headerSort: false,
        formatter: change,
      },
    ],
  });

  async function load({ append = false } = {}) {
    const params = new URLSearchParams({
      limit: String(PAGE),
      offset: String(append ? loaded.length : 0),
    });
    if (entitySelect.value) params.set("entity_type", entitySelect.value);
    let body;
    try {
      const resp = await fetch(`/web/api/audit?${params}`);
      if (!resp.ok) {
        showToast(await errorMessage(resp, "Could not read the audit log."));
        return;
      }
      body = await resp.json();
    } catch {
      showToast("Could not reach the server. Please try again.");
      return;
    }
    loaded = append ? loaded.concat(body.data) : body.data;
    more = body.more;
    await table.replaceData(loaded);
    moreBtn.hidden = !more;
    // "The 200 most recent" rather than a total: counting the whole log on
    // every page load buys a number nobody acts on.
    countEl.textContent = loaded.length
      ? `${loaded.length} entr${loaded.length === 1 ? "y" : "ies"}${more ? ", more to show" : ""}`
      : "";
  }

  entitySelect.addEventListener("change", () => load());
  moreBtn.addEventListener("click", () => load({ append: true }));
  load();
})();

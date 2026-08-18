// The audit log (admin only, spec §19). A Tabulator over /web/api/audit, which
// hands back rows already in words — the log's own field names are terse and
// parameterised ("quantity@location:5"), and turning those into English is the
// server's job, next to the parsers that built them.
//
// The column filters narrow the QUERY, not the rows on screen. This page holds
// a window onto a log that is walked a page at a time, so a filter applied to
// what is loaded would answer "nothing" for an entry sitting one page further
// back — the one failure that would make the log worth less than no log.
//
// Read-only by design: an audit trail that can be edited from the app it audits
// is not one. `esc` comes from shared.js.
(() => {
  const mount = document.getElementById("audit-table");
  if (!mount) return;

  const moreBtn = document.getElementById("audit-more");
  const clearBtn = document.getElementById("audit-clear");
  const countEl = document.getElementById("audit-count");
  const PAGE = 200;

  // Which header filter drives which query parameter.
  const FILTER_PARAM = {
    who_id: "who",
    entity_kind: "entity_type",
    what: "field",
    change: "value",
  };

  let loaded = [];
  let more = false;
  let reloading = false;

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

  // Every filtered column keeps all its rows: the server has already chosen
  // them, and a local filter on top would hide some of what it chose.
  const serverFilter = { headerFilterFunc: () => true };

  function listFilter(values) {
    return {
      ...serverFilter,
      headerFilter: "list",
      headerFilterParams: { values, clearable: true },
    };
  }

  function textFilter(title) {
    return {
      ...serverFilter,
      headerFilter: "input",
      headerFilterPlaceholder: `Filter ${title}…`,
      headerFilterParams: {
        elementAttributes: { "aria-label": `Filter ${title}` },
      },
    };
  }

  function auditColumns() {
    const kinds = JSON.parse(mount.dataset.kinds || "[]");
    const actors = JSON.parse(mount.dataset.actors || "[]");
    return [
      {
        title: "When",
        field: "when",
        width: 170,
        formatter: (cell) =>
          `<span class="cell-mono">${esc(cell.getValue().replace("T", " "))}</span>`,
      },
      {
        title: "Who",
        field: "who",
        width: 150,
        // Filtered by id and shown by name: an account can be renamed into
        // another's history, and an id cannot.
        headerFilterField: "who_id",
        ...listFilter(
          Object.fromEntries(actors.map((actor) => [actor.id, actor.name])),
        ),
      },
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
        headerFilterField: "entity_kind",
        ...listFilter(
          Object.fromEntries(
            kinds.map((kind) => [kind, kind.replace(/_/g, " ")]),
          ),
        ),
      },
      { title: "Field", field: "what", width: 220, ...textFilter("field") },
      {
        title: "Change",
        field: "change",
        headerSort: false,
        formatter: change,
        ...textFilter("change"),
      },
    ];
  }

  const table = new Tabulator(mount, {
    layout: "fitDataFill",
    placeholder: "Nothing recorded yet",
    columns: auditColumns(),
  });

  function activeFilters() {
    const active = {};
    for (const filter of table.getHeaderFilters()) {
      const param = FILTER_PARAM[filter.field];
      if (param && filter.value !== "" && filter.value != null) {
        active[param] = String(filter.value);
      }
    }
    return active;
  }

  async function load({ append = false } = {}) {
    const filters = activeFilters();
    const params = new URLSearchParams({
      ...filters,
      limit: String(PAGE),
      offset: String(append ? loaded.length : 0),
    });
    clearBtn.hidden = Object.keys(filters).length === 0;
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
    // Guarded, because replacing the data makes Tabulator re-run its filters,
    // which is what called this in the first place.
    reloading = true;
    try {
      await table.replaceData(loaded);
    } finally {
      reloading = false;
    }
    moreBtn.hidden = !more;
    // "The 200 most recent" rather than a total: counting the whole log on
    // every page load buys a number nobody acts on.
    countEl.textContent = loaded.length
      ? `${loaded.length} entr${loaded.length === 1 ? "y" : "ies"}${
          more ? ", more to show" : ""
        }`
      : "";
  }

  // A changed filter restarts the walk from the newest entry rather than
  // narrowing the window already on screen.
  table.on("dataFiltering", () => {
    if (reloading) return;
    load();
  });
  clearBtn.addEventListener("click", () => {
    table.clearHeaderFilter(); // fires dataFiltering, which reloads
  });
  moreBtn.addEventListener("click", () => load({ append: true }));
  load();
})();

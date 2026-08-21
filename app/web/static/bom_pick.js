// "Assign a component" picker for a BOM line (spec §21). A BOM matches lines to
// inventory by MPN; this is how a human overrides that — "whatever the CSV says,
// this line is built from THAT part" — for a line with no MPN, one nobody stocks,
// or one that ran out.
//
// Deliberately its own small table rather than the components page's: app.js is a
// page-scoped singleton (it reads #type-filter at load and would throw here), its
// `table`/`loadTable` are lexical globals that would collide, and it shares one
// localStorage column-width bucket keyed by field name alone. What IS reused is the
// feed — /web/api/components?type_id=N already returns the columns for a type,
// parameter columns included — so this only has to drop the ones that don't help
// you choose a part. `esc`, `csrfToken` and `errorMessage` come from shared.js.

(function () {
  const dialog = document.getElementById("bom-pick-dialog");
  if (!dialog) return; // read-only: the picker isn't rendered

  const refsEl = document.getElementById("bom-pick-refs");
  const factsEl = document.getElementById("bom-pick-facts");
  const selectedEl = document.getElementById("bom-pick-selected");
  const confirmBtn = document.getElementById("bom-pick-confirm");
  const typeSelect = document.getElementById("bom-pick-type");
  const errorEl = document.getElementById("bom-pick-error");
  const mount = document.getElementById("bom-pick-table");

  // Identity columns the caller already knows (they describe the BOM line, shown on
  // the left) or that don't help pick a substitute. What's left — package, mounting,
  // stock and the type's own parameters — is what you actually choose on.
  const HIDDEN_FIELDS = new Set(["type", "manufacturer", "mpn", "notes"]);

  let table = null;
  let line = null; // the BOM line being assigned
  let bomId = null;
  let selected = null; // the chosen component row
  let onAssigned = null;

  function setError(text) {
    errorEl.textContent = text || "";
    errorEl.hidden = !text;
  }

  function setSelected(row) {
    selected = row;
    confirmBtn.disabled = !row;
    // Echo what's about to be committed. The MPN column is hidden in the table, so
    // this is where the part is actually named.
    selectedEl.textContent = row
      ? `Selected: ${row.mpn || `#${row.id}`}${row.package ? ` · ${row.package}` : ""}`
      : "Select a component below.";
  }

  // The BOM line's own facts, so the choice is made against them without leaving.
  function renderFacts() {
    const rows = [
      ["Value", line.value],
      ["Category", line.category],
      ["Footprint", line.footprint],
      ["MPN in CSV", line.mpn],
      ["Qty per board", line.quantity],
      ["Needed", line.total_quantity],
    ];
    factsEl.innerHTML = rows
      .map(
        ([label, value]) =>
          `<dt>${esc(label)}</dt><dd>${value ? esc(String(value)) : "—"}</dd>`,
      )
      .join("");
  }

  // A Details link opens in a NEW TAB on purpose: navigating away would lose the
  // picker, and coming back with the browser's Back button would not restore the
  // list, the type filter or the scroll position.
  function detailsColumn() {
    return {
      title: "",
      field: "_details",
      headerSort: false,
      width: 90,
      hozAlign: "right",
      formatter: (cell) =>
        `<a class="btn btn-ghost btn-sm" target="_blank" rel="noopener"` +
        ` href="/components/${Number(cell.getRow().getData().id)}">Details</a>`,
    };
  }

  function pickerColumns(feedColumns) {
    const columns = feedColumns
      .filter((column) => !HIDDEN_FIELDS.has(column.field))
      .map((column) => ({
        title: column.title,
        field: column.field,
        headerFilter: "input",
        headerFilterPlaceholder: `Filter ${column.title}…`,
        ...(column.numeric ? { sorter: "number" } : {}),
      }));
    columns.push(detailsColumn());
    return columns;
  }

  async function loadInventory() {
    setError("");
    const typeId = typeSelect.value;
    const query = typeId ? `?type_id=${encodeURIComponent(typeId)}` : "";
    let payload;
    try {
      payload = await fetch(`/web/api/components${query}`).then((r) => r.json());
    } catch {
      setError("Could not load the inventory.");
      return;
    }
    setSelected(null);
    const columns = pickerColumns(payload.columns);
    if (!table) {
      // Columns AND rows go in at construction: a table built empty and filled a
      // tick later has nothing to size itself against inside a dialog.
      table = new Tabulator(mount, {
        layout: "fitDataFill",
        placeholder: "No components",
        selectableRows: 1,
        columns,
        data: payload.data,
      });
      table.on("rowClick", (event, row) => {
        if (event.target.closest("a")) return; // the Details link acts on its own
        row.select(); // selectableRows:1 deselects the previous row for us
        setSelected(row.getData());
      });
      // A double-click is the shortcut for "this one" — same as picking it and
      // pressing the button.
      table.on("rowDblClick", (event, row) => {
        if (event.target.closest("a")) return;
        row.select();
        setSelected(row.getData());
        confirm();
      });
    } else {
      // The type changed: its parameter columns differ, so both have to be replaced.
      table.setColumns(columns);
      await table.setData(payload.data);
    }
  }

  async function confirm() {
    if (!selected) return;
    setError("");
    confirmBtn.disabled = true;
    try {
      const resp = await fetch(
        `/api/boms/${bomId}/lines/${line.id}/component`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ component_id: selected.id }),
        },
      );
      if (!resp.ok) {
        setError(await errorMessage(resp));
        confirmBtn.disabled = false;
        return;
      }
    } catch {
      setError("Could not reach the server.");
      confirmBtn.disabled = false;
      return;
    }
    dialog.close();
    if (onAssigned) await onAssigned();
  }

  // Open the picker for one BOM line. `line` is the report row (it carries the
  // line id, its designators and the facts shown on the left); `onDone` refreshes
  // the report once the assignment lands.
  async function openBomPicker(reportLine, id, onDone) {
    if (dialog.open) return;
    line = reportLine;
    bomId = id;
    onAssigned = onDone || null;
    refsEl.textContent = reportLine.references || "";
    renderFacts();
    setError("");
    setSelected(null);
    // Start on the line's own category when it names a type we have — the common
    // case — but leave it changeable: the whole point is picking something else.
    const wanted = (reportLine.category || "").trim().toLowerCase();
    const match = [...typeSelect.options].find(
      (option) => option.textContent.trim().toLowerCase() === wanted,
    );
    typeSelect.value = match ? match.value : "";
    dialog.showModal();
    await loadInventory();
    // Built (or re-filled) while the dialog was closed or mid-open, a Tabulator has
    // no width to lay out against; redraw once it's actually on screen.
    table?.redraw(true);
  }
  window.openBomPicker = openBomPicker;

  typeSelect.addEventListener("change", loadInventory);
  confirmBtn.addEventListener("click", confirm);
})();

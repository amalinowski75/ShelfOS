// Invoice list table (spec §16). A Tabulator with the same per-column live text
// filters and sorting as the component table (app.js), so both lists behave and
// look alike. Data comes from the JSON feed /web/api/invoices; money is
// pre-formatted server-side (exact Decimal) and only sorted/filtered here.
// `esc` comes from shared.js.

// Parse the leading number out of a formatted money cell ("12.50 EUR"). A blank
// gross ("—" for a draft) has no amount, so it sorts below every real value.
function invoiceMoneyValue(text) {
  const n = parseFloat(String(text).replace(/[^0-9.-]/g, ""));
  return Number.isNaN(n) ? -Infinity : n;
}

function invoiceMoneySorter(a, b) {
  return invoiceMoneyValue(a) - invoiceMoneyValue(b);
}

function invoiceStatusBadge(value) {
  const cls = value === "finalized" ? "b-ok" : "b-neutral";
  return `<span class="badge ${cls}"><span class="dot"></span>${esc(value)}</span>`;
}

// Give a column the same live text header filter the component table uses:
// Tabulator's "input" filter (default "like" func) is a case-insensitive
// substring match applied as you type, and active filters AND across columns.
function invoiceFilter(column) {
  return {
    ...column,
    headerFilter: "input",
    headerFilterPlaceholder: `Filter ${column.title}…`,
    headerFilterParams: {
      elementAttributes: { "aria-label": `Filter ${column.title}` },
    },
  };
}

function invoiceColumns() {
  return [
    invoiceFilter({
      title: "Invoice",
      field: "invoice_number",
      formatter: (cell) => {
        const row = cell.getRow().getData();
        return `<a class="cell-mono" href="/invoices/${row.id}">${esc(cell.getValue())}</a>`;
      },
    }),
    invoiceFilter({ title: "Supplier", field: "supplier" }),
    invoiceFilter({ title: "Date", field: "invoice_date" }),
    invoiceFilter({
      title: "Net",
      field: "net",
      hozAlign: "right",
      sorter: invoiceMoneySorter,
    }),
    invoiceFilter({
      title: "Gross",
      field: "gross",
      hozAlign: "right",
      sorter: invoiceMoneySorter,
    }),
    invoiceFilter({
      title: "Status",
      field: "status",
      formatter: (cell) => invoiceStatusBadge(cell.getValue()),
    }),
  ];
}

async function loadInvoices(table, hint) {
  try {
    const feed = await fetch("/web/api/invoices").then((r) => r.json());
    await table.setData(feed.data);
    if (hint && feed.truncated) {
      hint.textContent = `Showing the ${feed.limit} most recent invoices.`;
      hint.hidden = false;
    }
  } catch {
    // Clear Tabulator's "loading" state and tell the user, rather than leaving
    // the table spinning forever on a network/parse failure.
    await table.setData([]);
    if (hint) {
      hint.textContent = "Could not load invoices — refresh to try again.";
      hint.className = "error";
      hint.hidden = false;
    }
  }
}

const invoicesContainer = document.getElementById("invoices-table");
if (invoicesContainer) {
  const invoicesTable = new Tabulator("#invoices-table", {
    layout: "fitColumns",
    placeholder: "No invoices",
    columns: invoiceColumns(),
  });
  const invoiceHint = document.getElementById("invoice-list-hint");
  invoicesTable.on("tableBuilt", () => loadInvoices(invoicesTable, invoiceHint));
}

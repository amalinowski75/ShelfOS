// Invoice workflow (spec §16): create/edit invoices, manage their lines and
// finalize. Uses shared.js helpers (csrfToken, esc, errorMessage). Loaded on
// both the invoice list and detail pages; each block guards on its own markup,
// which is only rendered for accounts that may write to a draft invoice.

async function sendJSON(url, method, payload) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(payload),
  });
}

function showError(el, message) {
  el.textContent = message;
  el.hidden = false;
}

// A single in-flight write at a time for this script's own forms, enough to stop
// a fast double-click sending a duplicate POST/PUT/DELETE. The shared component
// dialog (which can open stacked on top of the line dialog) runs its own submit
// with its own error handling and does not share this flag.
let inFlight = false;
async function guard(run) {
  if (inFlight) return;
  inFlight = true;
  try {
    await run();
  } finally {
    inFlight = false;
  }
}

// [data-close] buttons are wired once in shared.js.

// ---- New invoice (list page) -----------------------------------------------
const newInvoiceBtn = document.getElementById("invoice-new-btn");
if (newInvoiceBtn) {
  const dialog = document.getElementById("invoice-new-dialog");
  const form = document.getElementById("invoice-new-form");
  const error = document.getElementById("invoice-new-error");

  newInvoiceBtn.addEventListener("click", () => {
    form.reset();
    error.hidden = true;
    dialog.showModal();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    guard(async () => {
      const resp = await sendJSON("/api/invoices", "POST", {
        supplier: form.supplier.value.trim(),
        invoice_number: form.invoice_number.value.trim(),
        invoice_date: form.invoice_date.value,
        currency: form.currency.value.trim(),
        notes: form.notes.value.trim() || null,
      });
      if (resp.ok) {
        const created = await resp.json();
        window.location = `/invoices/${created.id}`; // straight to its detail page
      } else {
        showError(error, await errorMessage(resp));
      }
    });
  });
}

// ---- Detail page: metadata, lines, finalize --------------------------------
// The edit controls (and their dialogs) are only rendered for a writer viewing
// a draft; on a finalized or read-only page there is nothing to wire up.
const detail = document.getElementById("invoice-detail");
const lineDialog = document.getElementById("invoice-line-dialog");
if (detail && lineDialog) {
  const invoiceId = detail.dataset.invoiceId;

  // --- Edit metadata ---
  const metaBtn = document.getElementById("invoice-edit-btn");
  const metaDialog = document.getElementById("invoice-meta-dialog");
  const metaForm = document.getElementById("invoice-meta-form");
  const metaError = document.getElementById("invoice-meta-error");

  metaBtn.addEventListener("click", () => {
    metaForm.supplier.value = detail.dataset.supplier;
    metaForm.invoice_number.value = detail.dataset.invoiceNumber;
    metaForm.invoice_date.value = detail.dataset.invoiceDate;
    metaForm.currency.value = detail.dataset.currency;
    metaForm.notes.value = detail.dataset.notes;
    metaError.hidden = true;
    metaDialog.showModal();
  });

  metaForm.addEventListener("submit", (event) => {
    event.preventDefault();
    guard(async () => {
      const resp = await sendJSON(`/api/invoices/${invoiceId}`, "PATCH", {
        supplier: metaForm.supplier.value.trim(),
        invoice_number: metaForm.invoice_number.value.trim(),
        invoice_date: metaForm.invoice_date.value,
        currency: metaForm.currency.value.trim(),
        // `null` means "unchanged" server-side, so send it only when the field
        // is untouched; when the user edited (or cleared) it, send the literal
        // value — an empty string actually blanks the notes.
        notes:
          metaForm.notes.value === detail.dataset.notes
            ? null
            : metaForm.notes.value.trim(),
      });
      if (resp.ok) {
        window.location.reload();
      } else {
        showError(metaError, await errorMessage(resp));
      }
    });
  });

  // --- Line dialog (shared by add and edit) ---
  const lineForm = document.getElementById("invoice-line-form");
  const lineError = document.getElementById("invoice-line-error");
  const lineTitle = document.getElementById("invoice-line-title");
  const componentField = document.getElementById("line-component-field");
  const componentSelect = lineForm.component_id;

  // Fetch the component list once and reuse it for the picker.
  let componentOptions = null;
  async function loadComponentOptions() {
    if (componentOptions) return componentOptions;
    const payload = await fetch("/web/api/components").then((r) => r.json());
    componentOptions = payload.data.map((row) => ({
      id: row.id,
      label:
        (row.mpn || `#${row.id}`) +
        (row.manufacturer ? ` · ${row.manufacturer}` : "") +
        ` (${row.type})`,
    }));
    return componentOptions;
  }

  function fillComponentSelect(options, selectedId) {
    componentSelect.replaceChildren();
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = opt.id;
      o.textContent = opt.label;
      componentSelect.appendChild(o);
    }
    if (selectedId != null) componentSelect.value = String(selectedId);
  }

  async function openAddLine() {
    lineForm.reset();
    lineError.hidden = true;
    lineTitle.textContent = "Add line";
    lineForm.line_id.value = "";
    lineForm.dataset.originalLocationId = "";
    lineForm.dataset.originalSpn = "";
    componentField.hidden = false;
    componentSelect.required = true;
    const options = await loadComponentOptions();
    if (!options.length) {
      showError(lineError, "No components yet — use “New component” to add one.");
    }
    fillComponentSelect(options);
    lineDialog.showModal();
  }

  // Create a component without leaving the add-line flow: the shared dialog
  // opens on top, and on success the new component is loaded into the picker
  // and pre-selected.
  const addComponentBtn = document.getElementById("invoice-add-component-btn");
  if (addComponentBtn && window.openComponentDialog) {
    addComponentBtn.addEventListener("click", () => {
      openComponentDialog((created) => {
        // Insert the new component directly and select it — don't depend on a
        // reload that might not yet include it (and could reject). Invalidate
        // the cache so a later re-open re-fetches the canonical, fuller list.
        componentOptions = null;
        const label =
          (created.mpn || `#${created.id}`) +
          (created.manufacturer ? ` · ${created.manufacturer}` : "");
        if (!componentSelect.querySelector(`option[value="${created.id}"]`)) {
          componentSelect.appendChild(new Option(label, created.id));
        }
        componentSelect.value = String(created.id);
        lineError.hidden = true;
      });
    });
  }

  function openEditLine(row) {
    lineForm.reset();
    lineError.hidden = true;
    lineTitle.textContent = "Edit line";
    // The line's component is fixed on edit (the update endpoint does not move
    // a line to a different component), so the picker is hidden.
    componentField.hidden = true;
    componentSelect.required = false;
    lineForm.line_id.value = row.dataset.lineId;
    lineForm.quantity.value = row.dataset.quantity;
    lineForm.unit_price.value = row.dataset.unitPrice;
    lineForm.supplier_part_number.value = row.dataset.spn;
    lineForm.location_id.value = row.dataset.locationId;
    lineForm.dataset.originalLocationId = row.dataset.locationId;
    lineForm.dataset.originalSpn = row.dataset.spn;
    lineDialog.showModal();
  }

  lineForm.addEventListener("submit", (event) => {
    event.preventDefault();
    guard(async () => {
      const lineId = lineForm.line_id.value;
      const quantity = Number(lineForm.quantity.value);
      // Sent as a string so the server keeps the exact decimal (no float drift).
      const unitPrice = lineForm.unit_price.value;
      const locationValue = lineForm.location_id.value;

      if (!lineId) {
        const resp = await sendJSON(`/api/invoices/${invoiceId}/lines`, "POST", {
          component_id: Number(componentSelect.value),
          quantity,
          unit_price: unitPrice,
          supplier_part_number: lineForm.supplier_part_number.value.trim() || null,
          location_id: locationValue ? Number(locationValue) : null,
        });
        if (resp.ok) return window.location.reload();
        return showError(lineError, await errorMessage(resp));
      }

      // Edit. The location endpoint can only *set* a slot, not clear one, so
      // reject a clear attempt up front — before any write — rather than
      // silently ignoring it and reloading to the unchanged location.
      const originalLocation = lineForm.dataset.originalLocationId || "";
      if (!locationValue && originalLocation) {
        return showError(
          lineError,
          "A line's location can't be cleared once set — pick a location or leave it unchanged.",
        );
      }

      const resp = await sendJSON(
        `/api/invoices/${invoiceId}/lines/${lineId}`,
        "PUT",
        {
          quantity,
          unit_price: unitPrice,
          // As with notes: `null` = unchanged, so only send the part number when
          // it was actually edited; an empty string then clears it.
          supplier_part_number:
            lineForm.supplier_part_number.value === lineForm.dataset.originalSpn
              ? null
              : lineForm.supplier_part_number.value.trim(),
        },
      );
      if (!resp.ok) return showError(lineError, await errorMessage(resp));

      // The line-update endpoint does not carry location; apply a change via
      // the dedicated endpoint only when a different concrete slot was chosen.
      if (locationValue && locationValue !== originalLocation) {
        const locResp = await sendJSON(
          `/api/invoices/${invoiceId}/lines/${lineId}/location`,
          "PUT",
          { location_id: Number(locationValue) },
        );
        if (!locResp.ok) {
          // The field changes above already persisted; be honest that only the
          // location step failed rather than implying nothing was saved.
          return showError(
            lineError,
            `Line updated, but its location could not be set: ${await errorMessage(locResp)}`,
          );
        }
      }
      window.location.reload();
    });
  });

  const addLineBtn = document.getElementById("invoice-addline-btn");
  addLineBtn.addEventListener("click", openAddLine);

  // Per-row edit / remove, delegated from the lines table.
  document.querySelector("table.data")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const row = button.closest("tr");
    if (button.dataset.act === "edit-line") {
      openEditLine(row);
    } else if (button.dataset.act === "remove-line") {
      if (!window.confirm("Remove this line from the invoice?")) return;
      guard(async () => {
        const resp = await fetch(
          `/api/invoices/${invoiceId}/lines/${row.dataset.lineId}`,
          { method: "DELETE", headers: { "X-CSRF-Token": csrfToken } },
        );
        if (resp.ok) {
          window.location.reload();
        } else {
          window.alert(await errorMessage(resp));
        }
      });
    }
  });

  // --- Finalize ---
  const finalizeBtn = document.getElementById("invoice-finalize-btn");
  const finalizeDialog = document.getElementById("invoice-finalize-dialog");
  const finalizeForm = document.getElementById("invoice-finalize-form");
  const finalizeError = document.getElementById("invoice-finalize-error");

  // Only present when the draft has at least one line (see the template).
  if (finalizeBtn) {
    finalizeBtn.addEventListener("click", () => {
      finalizeForm.reset();
      finalizeError.hidden = true;
      finalizeDialog.showModal();
    });

    finalizeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      guard(async () => {
        const gross = finalizeForm.total_gross.value;
        const resp = await sendJSON(`/api/invoices/${invoiceId}/finalize`, "POST", {
          total_gross: gross ? gross : null,
        });
        if (resp.ok) {
          window.location.reload();
        } else {
          showError(finalizeError, await errorMessage(resp));
        }
      });
    });
  }
}

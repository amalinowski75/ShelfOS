// Test harness for the plain (non-module) browser scripts under app/web/static.
//
// Each call builds a fresh jsdom window, injects the given static files as
// classic <script> elements (so their top-level `const`/`function` land in the
// same global scope the browser gives them), and stubs the browser APIs the
// scripts reach for (fetch, dialogs, confirm/alert, navigation).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";
import { vi } from "vitest";

const STATIC = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "app",
  "web",
  "static",
);

const readStatic = (name) => readFileSync(join(STATIC, name), "utf8");

export const CSRF = "csrf-test-token";

export function loadPage(bodyHtml, scripts, { fetchImpl } = {}) {
  const virtualConsole = new VirtualConsole();
  // jsdom logs a "Not implemented: navigation" error for the reload/redirect
  // the scripts do on success; swallow only that specific message, and surface
  // every other jsdom error (including other unimplemented APIs) so a broken
  // script fails the test loudly.
  virtualConsole.on("jsdomError", (err) => {
    if (!/Not implemented: navigation/.test(err.message)) throw err;
  });

  const dom = new JSDOM(
    `<!DOCTYPE html><html><head><meta name="csrf-token" content="${CSRF}"></head>` +
      `<body>${bodyHtml}</body></html>`,
    { runScripts: "dangerously", virtualConsole, url: "http://localhost/" },
  );
  const { window } = dom;

  const fetchMock = vi.fn(
    fetchImpl ??
      (() => Promise.resolve({ ok: true, json: () => Promise.resolve({ id: 42 }) })),
  );
  window.fetch = fetchMock;
  window.confirm = vi.fn(() => true);
  window.alert = vi.fn();
  // jsdom does not implement <dialog> modality; the scripts only open/close.
  window.HTMLDialogElement.prototype.showModal = vi.fn();
  window.HTMLDialogElement.prototype.close = vi.fn();
  // app.js constructs a Tabulator at load; stub the few methods it calls so the
  // component-table code can run without the real (CDN) library.
  window.Tabulator = class {
    setColumns() {}
    setData() {
      return Promise.resolve();
    }
    on() {}
  };

  // Browsers expose form controls as named properties on the form
  // (``form.supplier``), which the scripts rely on; jsdom only implements
  // ``form.elements.supplier``. Bridge the two *before* injecting the scripts,
  // since some scripts capture a control (e.g. the component <select>) into a
  // module-level const at load time.
  patchFormNamedAccess(window.document);

  for (const name of scripts) {
    const el = window.document.createElement("script");
    el.textContent = readStatic(name);
    window.document.body.appendChild(el);
  }
  return { window, document: window.document, fetchMock };
}

function patchFormNamedAccess(document) {
  for (const form of document.querySelectorAll("form")) {
    for (const control of form.elements) {
      const name = control.getAttribute("name");
      if (name && !(name in form)) {
        Object.defineProperty(form, name, {
          configurable: true,
          get: () => form.elements.namedItem(name),
        });
      }
    }
  }
}

// Drain the task queue so awaited fetch handlers (including the line-edit path's
// two chained PUTs) run to completion. Two macrotask hops cover the chains.
export const tick = async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
};

// The body of the invoice list page (just the create-invoice controls).
export function newInvoiceFixture() {
  return `
    <button id="invoice-new-btn"></button>
    <dialog id="invoice-new-dialog"><form id="invoice-new-form">
      <input name="supplier" />
      <input name="invoice_number" />
      <input name="invoice_date" type="date" />
      <input name="currency" />
      <input name="notes" />
      <p id="invoice-new-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
}

// The shared "New location" dialog markup (mirrors _location_dialog.html);
// append to a page fixture to exercise the inline-create flow end to end.
export function locationDialogFixture() {
  return `
    <dialog id="location-dialog"><form id="location-form">
      <select name="type"><option value="rack">rack</option></select>
      <input name="name" />
      <select name="parent_id">
        <option value="">None (top level)</option>
        <option value="5">D1</option>
      </select>
      <p id="location-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
}

// The body of an editable (draft) invoice detail page, with one line.
export function detailFixture({
  notes = "",
  lineSpn = "",
  lineLocationId = "",
  withFinalize = false,
  secondLine = false,
} = {}) {
  const extraRow = secondLine
    ? `<tr data-line-id="4" data-quantity="2" data-unit-price="2.00"
           data-spn="" data-location-id="">
         <td>
           <button type="button" data-act="edit-line"></button>
           <button type="button" data-act="remove-line"></button>
         </td>
       </tr>`
    : "";
  const finalize = withFinalize
    ? `<button id="invoice-finalize-btn"></button>
       <dialog id="invoice-finalize-dialog"><form id="invoice-finalize-form">
         <input name="total_gross" />
         <p id="invoice-finalize-error" hidden></p>
         <button type="submit"></button>
       </form></dialog>`
    : "";

  return `
    <div id="invoice-detail"
         data-invoice-id="7"
         data-currency="EUR"
         data-supplier="Mouser"
         data-invoice-number="INV-1"
         data-invoice-date="2026-07-08"
         data-notes="${notes}">
      <button id="invoice-edit-btn"></button>
      <button id="invoice-addline-btn"></button>
      <table class="data"><tbody>
        <tr data-line-id="3"
            data-quantity="5"
            data-unit-price="1.50"
            data-spn="${lineSpn}"
            data-location-id="${lineLocationId}">
          <td>
            <button type="button" data-act="edit-line"></button>
            <button type="button" data-act="remove-line"></button>
          </td>
        </tr>
        ${extraRow}
      </tbody></table>
    </div>

    <dialog id="invoice-meta-dialog"><form id="invoice-meta-form">
      <input name="supplier" />
      <input name="invoice_number" />
      <input name="invoice_date" type="date" />
      <input name="currency" />
      <input name="notes" />
      <p id="invoice-meta-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>

    <dialog id="invoice-line-dialog"><form id="invoice-line-form">
      <input type="hidden" name="line_id" />
      <div id="line-component-field">
        <select name="component_id"></select>
        <button type="button" id="invoice-add-component-btn"></button>
      </div>
      <input name="quantity" type="number" />
      <input name="unit_price" type="number" />
      <input name="supplier_part_number" />
      <div class="loc-picker" data-optional="true">
        <input type="hidden" name="location_id" value="" />
        <button type="button" class="loc-picker-toggle">
          <span class="loc-picker-label">— none —</span>
        </button>
        <div class="loc-picker-menu" hidden>
          <button type="button" class="loc-picker-new" hidden>+ New location</button>
          <button type="button" class="loc-picker-node loc-picker-none" data-loc-id="" data-loc-path="">— none —</button>
          <ul class="loc-picker-list">
            <li><div class="loc-picker-row"><span class="loc-picker-caret-spacer"></span>
              <button type="button" class="loc-picker-node" data-loc-id="5" data-loc-path="D1">D1</button></div></li>
            <li><div class="loc-picker-row"><span class="loc-picker-caret-spacer"></span>
              <button type="button" class="loc-picker-node" data-loc-id="9" data-loc-path="D2">D2</button></div></li>
          </ul>
        </div>
      </div>
      <p id="invoice-line-error" hidden></p>
      <strong id="invoice-line-title"></strong>
      <button type="submit"></button>
    </form></dialog>
    ${finalize}`;
}

// The components page body app.js touches at load, plus the New Component
// dialog. Only the elements the script reads are included.
export function componentPageFixture(types = [{ id: 1, name: "resistor" }]) {
  const options = types
    .map((t) => `<option value="${t.id}">${t.name}</option>`)
    .join("");
  return `
    <select id="type-filter"><option value="">All types</option>${options}</select>
    <button id="new-component-btn"></button>
    <div id="components-table"></div>
    <dialog id="stock-dialog"><form id="stock-form"></form></dialog>
    <dialog id="component-dialog"><form id="component-form">
      <select name="type_id" id="component-type">
        <option value="">Select a type…</option>${options}
      </select>
      <input name="manufacturer" />
      <input name="mpn" />
      <input name="package" />
      <select name="mounting_type">
        <option value="SMT">SMT</option>
        <option value="THT">THT</option>
      </select>
      <input name="notes" />
      <p id="component-params-hint"></p>
      <div id="component-params"></div>
      <p id="component-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
}

// The shared "New component" dialog markup (mirrors _component_dialog.html),
// for pages that reuse it (e.g. the invoice add-line flow).
export function componentDialogFixture(types = [{ id: 1, name: "resistor" }]) {
  const options = types
    .map((t) => `<option value="${t.id}">${t.name}</option>`)
    .join("");
  return `
    <dialog id="component-dialog"><form id="component-form">
      <select name="type_id" id="component-type">
        <option value="">Select a type…</option>${options}
      </select>
      <input name="manufacturer" />
      <input name="mpn" />
      <input name="package" />
      <select name="mounting_type"><option value="Other" selected>Other</option></select>
      <input name="notes" />
      <p id="component-params-hint"></p>
      <div id="component-params"></div>
      <p id="component-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
}

// Parse the JSON body of the Nth fetch call.
export function fetchBody(fetchMock, call = 0) {
  return JSON.parse(fetchMock.mock.calls[call][1].body);
}

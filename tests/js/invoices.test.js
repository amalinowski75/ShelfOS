import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, it, expect, vi } from "vitest";
import {
  loadPage,
  tick,
  CSRF,
  newInvoiceFixture,
  invoiceImportFixture,
  detailFixture,
  componentDialogFixture,
  locationDialogFixture,
  fetchBody,
} from "./harness.js";

// location_tree.js enhances the line dialog's location picker (§7).
const SCRIPTS = ["shared.js", "location_tree.js", "invoices.js"];
// With the inline "+ New location" dialog wired in. location_dialog.js must load
// before location_tree.js so window.openLocationDialog exists at enhance time.
const SCRIPTS_INLINE_LOCATION = [
  "shared.js",
  "location_dialog.js",
  "location_tree.js",
  "invoices.js",
];

function submit(document, formId) {
  document
    .getElementById(formId)
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("invoices.js — new invoice", () => {
  it("posts the metadata and sends empty notes as null", async () => {
    const { document, fetchMock } = loadPage(newInvoiceFixture(), SCRIPTS);
    const form = document.getElementById("invoice-new-form");
    form.supplier.value = "Mouser";
    form.invoice_number.value = "INV-1";
    form.invoice_date.value = "2026-07-08";
    form.currency.value = "EUR";
    form.notes.value = "";

    submit(document, "invoice-new-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({
      supplier: "Mouser",
      invoice_number: "INV-1",
      invoice_date: "2026-07-08",
      currency: "EUR",
      notes: null,
    });
  });
});

describe("invoices.js — import from PDF", () => {
  it("uploads the file as multipart with CSRF and redirects to the draft", async () => {
    const { window, document, fetchMock } = loadPage(
      invoiceImportFixture(),
      SCRIPTS,
      {
        fetchImpl: () =>
          Promise.resolve({ ok: true, json: async () => ({ invoice_id: 42 }) }),
      },
    );
    submit(document, "invoice-import-form");
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/import");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    // Multipart: a FormData body, no JSON Content-Type header.
    expect(opts.body).toBeInstanceOf(window.FormData);
    expect(opts.headers["Content-Type"]).toBeUndefined();
    // Success path: no error surfaced (jsdom doesn't reflect the location
    // assignment, so the redirect itself isn't asserted here — the error branch
    // test below covers the failure handling that must NOT redirect).
    expect(document.getElementById("invoice-import-error").hidden).toBe(true);
  });

  it("surfaces the server error and re-enables the button on failure", async () => {
    const { document, fetchMock } = loadPage(invoiceImportFixture(), SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          json: async () => ({ detail: "unrecognised invoice" }),
        }),
    });
    submit(document, "invoice-import-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const error = document.getElementById("invoice-import-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("unrecognised invoice");
    const button = document.getElementById("invoice-import-submit");
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe("Import");
  });
});

describe("invoices.js — review imported lines inline", () => {
  function change(el) {
    el.dispatchEvent(
      new el.ownerDocument.defaultView.Event("change", { bubbles: true }),
    );
  }

  it("still opens Add line when the component list cannot be read", async () => {
    // The failure that used to be silent: `.data` came back undefined, `.map`
    // threw out of an async click handler with nobody to catch it, and the dialog
    // never opened — a dead button, no message, nothing in the page to act on.
    const page = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        Promise.resolve({
          ok: url !== "/web/api/components",
          json: async () =>
            url === "/web/api/components" ? { detail: "Server Error" } : {},
        }),
    });
    const opened = [];
    page.document.getElementById("invoice-line-dialog").showModal = () =>
      opened.push(true);

    page.document.getElementById("invoice-addline-btn").click();
    await tick();
    await tick();

    expect(opened.length).toBe(1); // the dialog is still the way out
    const error = page.document.getElementById("invoice-line-error");
    expect(page.document.getElementById("invoice-line-error-row").hidden).toBe(false);
    expect(error.textContent).toContain("Could not load");
    // The ACTION, not advice about it — three fixes running got the wording of
    // "how to retry" wrong, and a button beside the message cannot be.
    expect(page.document.getElementById("invoice-line-retry").hidden).toBe(false);
    expect(error.textContent).not.toMatch(/refresh|close and/);
  });

  it("says WHICH nothing it is — an empty catalog is not an unreadable one", async () => {
    const message = async (body) => {
      const page = loadPage(detailFixture(), SCRIPTS, {
        fetchImpl: () => Promise.resolve({ ok: true, json: async () => body }),
      });
      page.document.getElementById("invoice-line-dialog").showModal = () => {};
      page.document.getElementById("invoice-addline-btn").click();
      await tick();
      await tick();
      return page.document.getElementById("invoice-line-error").textContent;
    };

    expect(await message({ data: [] })).toContain("No components yet");
    // A 200 of the wrong shape is a failure, not an empty inventory: telling the
    // user they have no components is a worse thing to be wrong about.
    expect(await message({})).toContain("Could not load");
  });

  it("does not cache a failed load, so the next open tries again", async () => {
    let calls = 0;
    const page = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (url !== "/web/api/components") {
          return Promise.resolve({ ok: true, json: async () => ({}) });
        }
        calls += 1;
        return Promise.resolve({
          ok: true,
          json: async () =>
            calls === 1 ? {} : { data: [{ id: 1, mpn: "R", type: "t" }] },
        });
      },
    });
    page.document.getElementById("invoice-line-dialog").showModal = () => {};
    const btn = page.document.getElementById("invoice-addline-btn");

    btn.click();
    await tick();
    await tick();
    expect(page.document.getElementById("invoice-line-error").textContent).toContain(
      "Could not load",
    );

    btn.click();
    await tick();
    await tick();
    expect(calls).toBe(2);
    expect(page.document.getElementById("invoice-line-error-row").hidden).toBe(true);
    expect(
      page.document.querySelectorAll("#invoice-line-form select[name=component_id] option")
        .length,
    ).toBe(1);
  });

  it("Retry re-loads the component list in place, without reopening", async () => {
    // Why a button rather than a fourth attempt at the wording: the user stays
    // in the dialog, and the sentence no longer has to describe anything.
    let calls = 0;
    const page = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (url !== "/web/api/components") {
          return Promise.resolve({ ok: true, json: async () => ({}) });
        }
        calls += 1;
        return Promise.resolve({
          ok: true,
          json: async () =>
            calls === 1 ? {} : { data: [{ id: 1, mpn: "R", type: "t" }] },
        });
      },
    });
    const opened = [];
    page.document.getElementById("invoice-line-dialog").showModal = () =>
      opened.push(true);
    page.document.getElementById("invoice-addline-btn").click();
    await tick();
    await tick();

    const retry = page.document.getElementById("invoice-line-retry");
    expect(retry.hidden).toBe(false);
    retry.click();
    await tick();
    await tick();

    expect(calls).toBe(2);
    expect(page.document.getElementById("invoice-line-error-row").hidden).toBe(true);
    expect(retry.hidden).toBe(true);
    // The TEXT goes with the row. Left in the DOM it is one CSS mistake away from
    // being read out or shown again — which is exactly how the .error-row[hidden]
    // bug surfaced.
    expect(page.document.getElementById("invoice-line-error").textContent).toBe("");
    expect(
      page.document.querySelectorAll(
        "#invoice-line-form select[name=component_id] option",
      ).length,
    ).toBe(1);
    expect(opened.length).toBe(1); // still the same open — no reopen
  });

  it("announces the failure, since the message is the only instruction left", async () => {
    // "The user is already looking at it" is a sighted premise: the row appears
    // asynchronously inside an open modal and now carries the ONLY guidance on
    // recovering, the wording having been deliberately removed in favour of the
    // button. alert, not status: this reports a failure, not a state.
    const page = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        Promise.resolve({
          ok: true,
          json: async () => (url === "/web/api/components" ? { detail: "nope" } : {}),
        }),
    });
    page.document.getElementById("invoice-line-dialog").showModal = () => {};
    page.document.getElementById("invoice-addline-btn").click();
    await tick();
    await tick();

    const row = page.document.getElementById("invoice-line-error-row");
    expect(row.getAttribute("role")).toBe("alert");
    expect(row.hidden).toBe(false); // announced only once it is actually shown
  });

  it("offers no Retry for an empty catalog or a rejected save", async () => {
    // Retry is for a failed LOAD. Re-fetching the list neither fills an empty
    // inventory nor fixes a value the server refused, and a button that does
    // nothing useful is worse than none.
    const page = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        Promise.resolve({
          ok: true,
          json: async () => (url === "/web/api/components" ? { data: [] } : {}),
        }),
    });
    page.document.getElementById("invoice-line-dialog").showModal = () => {};
    page.document.getElementById("invoice-addline-btn").click();
    await tick();
    await tick();

    expect(page.document.getElementById("invoice-line-error").textContent).toContain(
      "No components yet",
    );
    expect(page.document.getElementById("invoice-line-retry").hidden).toBe(true);
  });

  it("PATCHes location_id (as null when blanked) on location change", async () => {
    const { document, fetchMock } = loadPage(detailFixture({ pending: true }), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }),
    });
    const locationSelect = document.querySelector("#invoice-review .ril-location");
    locationSelect.value = "5";
    change(locationSelect);
    await tick();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/import-lines/21");
    expect(opts.method).toBe("PATCH");
    expect(JSON.parse(opts.body)).toEqual({ location_id: 5 });
  });

  it("dismiss deletes the row after confirmation, with CSRF", async () => {
    const { window, document, fetchMock } = loadPage(
      detailFixture({ pending: true }),
      SCRIPTS,
      { fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }) },
    );
    document.querySelector('[data-act="dismiss-import"]').click();
    await tick();

    expect(window.confirm).toHaveBeenCalled();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/import-lines/21");
    expect(opts.method).toBe("DELETE");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("files a real line from its inline picker, and drops the placeholder", async () => {
    // The same quick action the review table has always offered. No reload: the
    // cell is all that changed, and re-rendering would throw away the scroll
    // position halfway down a long invoice.
    const { document, fetchMock, navigations } = loadPage(
      detailFixture({ lineLocationId: "" }),
      SCRIPTS,
      { fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }) },
    );
    const select = document.querySelector("#invoice-lines .line-location");
    expect(select.querySelector('option[value=""]')).toBeTruthy();

    select.value = "5";
    change(select);
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/lines/3/location");
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body)).toEqual({ location_id: 5 });
    // Once filed there is nothing to clear — the endpoint cannot take it back —
    // so the placeholder must stop offering it.
    expect(select.querySelector('option[value=""]')).toBe(null);
    expect(navigations.length).toBe(0);
  });

  it("puts the cell back where it was when the server refuses", async () => {
    // Leaving the rejected choice on screen would show a location the line does
    // not have, which is a tempting thing to act on.
    const { document, window } = loadPage(detailFixture({ lineLocationId: "5" }), SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({ ok: false, json: async () => ({ detail: "no such location" }) }),
    });
    const select = document.querySelector("#invoice-lines .line-location");
    select.value = "6";
    change(select);
    await tick();

    expect(window.alert).toHaveBeenCalled();
    expect(select.value).toBe("5");
  });

  it("keeps the evidence collapsed until asked for", async () => {
    // On a long invoice most lines are unremarkable; a list under every one of
    // them would bury the few that need a decision.
    const { document } = loadPage(detailFixture({ pending: true }), SCRIPTS);
    const chip = document.querySelector('[data-act="show-existing"]');
    const panel = document.getElementById("existing-21");
    expect(panel.hidden).toBe(true);

    chip.click();
    expect(panel.hidden).toBe(false);
    expect(chip.getAttribute("aria-expanded")).toBe("true");

    chip.click(); // and it closes again
    expect(panel.hidden).toBe(true);
    expect(chip.getAttribute("aria-expanded")).toBe("false");
  });

  it("“This is it” files the line against that component, with CSRF", async () => {
    const { document, fetchMock, navigations } = loadPage(
      detailFixture({ pending: true }),
      SCRIPTS,
      { fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }) },
    );
    document.querySelector('[data-act="adopt-existing"]').click();
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/import-lines/21/adopt");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({ component_id: 88 });
    // A reload, not a hand-moved row: the line has crossed from the review table
    // to the Lines table, and the totals and the pending count move with it.
    expect(navigations.length).toBe(1);
  });

  it("reports an adopt that the server refused, and does not reload", async () => {
    const { document, navigations } = loadPage(detailFixture({ pending: true }), SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          json: async () => ({ detail: "component not found" }),
        }),
    });
    document.querySelector('[data-act="adopt-existing"]').click();
    await tick();

    const error = document.getElementById("invoice-review-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("component not found");
    expect(navigations.length).toBe(0);
  });

  it("“Edit line” on a staged row opens the same dialog, filled from the row", async () => {
    const { document } = loadPage(detailFixture({ pending: true }), SCRIPTS);

    // A REAL line first, so line_id is actually set — form.reset() does not clear
    // a hidden input (setting .value writes the content attribute, and reset
    // restores exactly that), so the staged open has to clear it itself.
    document.querySelector('#invoice-lines [data-act="edit-line"]').click();
    document.querySelector('[data-act="edit-import-line"]').click();

    const form = document.getElementById("invoice-line-form");
    expect(document.getElementById("invoice-line-title").textContent).toBe("Edit line");
    // A staged row has no component to move it between, so that picker stays away.
    expect(document.getElementById("line-component-field").hidden).toBe(true);
    expect(form.quantity.value).toBe("7");
    expect(form.unit_price.value).toBe("2.50");
    expect(form.supplier_part_number.value).toBe("SPN-2");
    expect(form.dataset.importLineId).toBe("21");
    expect(form.line_id.value).toBe(""); // not a real line — the submit path forks on this
  });

  it("saves a staged row's line edits in ONE patch, location included", async () => {
    const { document, fetchMock } = loadPage(detailFixture({ pending: true }), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }),
    });
    document.querySelector('[data-act="edit-import-line"]').click();
    const form = document.getElementById("invoice-line-form");
    form.quantity.value = "9";
    form.unit_price.value = "3.75";
    form.supplier_part_number.value = "SPN-EDITED";
    form.location_id.value = "5";
    submit(document, "invoice-line-form");
    await tick();

    // One request, not the line-then-location pair a REAL line needs: nothing is
    // committed to stock yet, so update_pending takes the location too.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/import-lines/21");
    expect(opts.method).toBe("PATCH");
    expect(JSON.parse(opts.body)).toEqual({
      quantity: 9,
      unit_price: "3.75",
      supplier_part_number: "SPN-EDITED",
      location_id: 5,
    });
  });

  it("lets a staged row clear its location, which a real line may not", async () => {
    // The asymmetry that is real rather than accidental: a real line's endpoint
    // only ASSIGNS a slot, but a staged row has committed nothing to protect.
    const { document, fetchMock } = loadPage(detailFixture({ pending: true }), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }),
    });
    document.querySelector('[data-act="edit-import-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "";
    submit(document, "invoice-line-form");
    await tick();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).location_id).toBe(null);
    expect(document.getElementById("invoice-line-error-row").hidden).toBe(true);
  });

  it("does not carry one row's mode into the next open of the shared dialog", async () => {
    // One dialog serves three jobs — add a line, edit a line, edit a staged row —
    // and it forks on two markers. A marker left behind by the previous open
    // sends the next save to the wrong endpoint, writing one row's edits onto
    // another. Both directions, since only one of them is caught by the fork's
    // own ordering.
    const { document, fetchMock } = loadPage(
      detailFixture({ pending: true, lineLocationId: "5" }),
      SCRIPTS,
      {
        fetchImpl: (url) =>
          Promise.resolve({
            ok: true,
            json: async () =>
              url === "/web/api/components"
                ? { data: [{ id: 1, mpn: "X", manufacturer: "A", type: "t" }] }
                : {},
          }),
      },
    );

    // Staged first, then the real line: the real line must not PATCH the staged row.
    document.querySelector('[data-act="edit-import-line"]').click();
    document.querySelector('#invoice-lines [data-act="edit-line"]').click();
    submit(document, "invoice-line-form");
    await tick();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/lines/3");

    // And back the other way.
    fetchMock.mockClear();
    document.querySelector('[data-act="edit-import-line"]').click();
    submit(document, "invoice-line-form");
    await tick();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/import-lines/21");
    // …and the real line's location did not come with it: the staged row has
    // none, so null is the right answer and proves the picker is reset too.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).location_id).toBe(null);

    // And "Add line", which shares the dialog with both of them: a marker left
    // behind here would PATCH the staged row instead of creating a line.
    fetchMock.mockClear();
    document.getElementById("invoice-addline-btn").click();
    await tick();
    submit(document, "invoice-line-form");
    await tick();
    const writes = fetchMock.mock.calls.filter(([, opts]) => opts?.method);
    expect(writes.at(-1)[0]).toBe("/api/invoices/7/lines");
  });

  it("does not wipe a location the inline picker just set", async () => {
    // The bug this shape invites, and the one none of the other tests could see:
    // they each exercise one entry point from a freshly rendered page. Here the
    // reviewer sets a shelf inline, then opens Edit line to fix a price — and the
    // staged PATCH sends location_id unconditionally, so a stale row attribute is
    // not merely displayed, it is SAVED over the real value.
    const { document, fetchMock } = loadPage(detailFixture({ pending: true }), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }),
    });
    const row = document.querySelector("#invoice-review tr[data-import-line-id]");
    const inline = row.querySelector(".ril-location");
    inline.value = "5";
    change(inline);
    await tick();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ location_id: 5 });

    document.querySelector('[data-act="edit-import-line"]').click();
    // The dialog opens on the location that is actually set…
    expect(document.getElementById("invoice-line-form").location_id.value).toBe("5");
    submit(document, "invoice-line-form");
    await tick();
    // …and saving anything else keeps it.
    expect(JSON.parse(fetchMock.mock.calls.at(-1)[1].body).location_id).toBe(5);
  });

  it("keeps a real line's row in step with its inline picker too", async () => {
    // Milder on this side — the real-line submit only fires the location PUT when
    // the value CHANGED, so a stale attribute shows a wrong answer rather than
    // saving one. Still a wrong answer next to an editable control.
    const { document } = loadPage(detailFixture({ lineLocationId: "" }), SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }),
    });
    const select = document.querySelector("#invoice-lines .line-location");
    select.value = "5";
    change(select);
    await tick();

    expect(select.closest("tr").dataset.locationId).toBe("5");
    document.querySelector('#invoice-lines [data-act="edit-line"]').click();
    expect(document.getElementById("invoice-line-form").location_id.value).toBe("5");
  });

  it("never drops a pick made while another is still in flight", async () => {
    // `guard` is page-wide and DISCARDS whatever arrives mid-request. For a submit
    // button that is right — the second click is the same action twice. For these
    // it is not: the second change is a different row's shelf, and dropping it
    // leaves the select showing a location the server never received. Working
    // down a long invoice row by row is exactly this motion.
    const release = [];
    const { document, fetchMock } = loadPage(
      detailFixture({ pending: true, secondPending: true }),
      SCRIPTS,
      {
        fetchImpl: () =>
          new Promise((r) =>
            release.push(() => r({ ok: true, json: async () => ({}) })),
          ),
      },
    );
    const selects = [...document.querySelectorAll("#invoice-review .ril-location")];
    expect(selects.length).toBe(2);

    selects[0].value = "5";
    change(selects[0]);
    selects[1].value = "5";
    change(selects[1]); // arrives while the first is still open

    await tick();
    expect(fetchMock.mock.calls.length).toBe(2); // neither was thrown away
    release.forEach((f) => f());
    await tick();
  });

  it("files the last of several quick picks on one control, and only that one", async () => {
    // Same rule on the other table, and the shape it takes on a SINGLE control:
    // under the page-wide flag the second pick vanishes and the cell shows a shelf
    // nobody was told about. Queued, the user's final choice is filed — once,
    // rather than replaying every shelf they passed through with an audit row each.
    const release = [];
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "" }),
      SCRIPTS,
      {
        fetchImpl: () =>
          new Promise((r) =>
            release.push(() => r({ ok: true, json: async () => ({}) })),
          ),
      },
    );
    const select = document.querySelector("#invoice-lines .line-location");

    select.value = "5";
    change(select);
    select.value = "6";
    change(select); // arrives while the first is still open

    await tick();
    await tick();

    // One request, carrying 6 — not two, and not the abandoned 5.
    expect(fetchMock.mock.calls.length).toBe(1);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).location_id).toBe(6);

    // And the superseded run really stood down rather than merely waiting its
    // turn: letting the queue drain adds nothing.
    release.shift()();
    await tick();
    await tick();
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("waits for a pick already in flight instead of racing it", async () => {
    // Once a request has STARTED it is never disowned — the next pick queues
    // behind it, so the server sees the two writes in the order they were made
    // rather than whichever round trip happens to finish first.
    const release = [];
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "" }),
      SCRIPTS,
      {
        fetchImpl: () =>
          new Promise((r) =>
            release.push(() => r({ ok: true, json: async () => ({}) })),
          ),
      },
    );
    const select = document.querySelector("#invoice-lines .line-location");

    select.value = "5";
    change(select);
    await tick(); // the first request is now open

    select.value = "6";
    change(select);
    await tick();
    expect(fetchMock.mock.calls.length).toBe(1); // the second is holding

    release.shift()();
    await tick();
    await tick();

    expect(fetchMock.mock.calls.map(([, o]) => JSON.parse(o.body).location_id)).toEqual([
      5, 6,
    ]);
    release.forEach((f) => f());
    await tick();
  });

  it("Edit opens the New Component dialog in stage mode with the row's prefill", async () => {
    const { window, document } = loadPage(detailFixture({ pending: true }), SCRIPTS);
    // The component dialog is a shared global; stub it to capture the call.
    const calls = [];
    window.openComponentDialog = (cb, prefill, opts) =>
      calls.push({ cb, prefill, opts });

    document.querySelector('[data-act="edit-import"]').click();

    expect(calls).toHaveLength(1);
    expect(calls[0].prefill).toEqual({
      typeId: "3",
      mpn: "ABC123",
      manufacturer: "Acme",
      package: "SOT23",
      mountingType: "THT",
      notes: "A widget",
      paramValues: [{ parameter_definition_id: 9, value: "4k7" }],
      // The distributor product link (built server-side from shop + SPN) is passed
      // through so the dialog's "Open in shop" button can target it.
      shopUrl: "https://www.tme.eu/en/details/ABC123/",
      // The shop and its own number: what a type correction re-asks the shop with.
      shopKey: "tme",
      supplierPartNumber: "SPN-2",
    });
    expect(calls[0].opts).toEqual({
      stage: { invoiceId: "7", importLineId: "21" },
    });
    expect(typeof calls[0].cb).toBe("function"); // the reload callback
  });
});

describe("invoices.js — edit metadata", () => {
  it("sends null for untouched notes but an explicit '' when cleared", async () => {
    const untouched = loadPage(detailFixture({ notes: "rush" }), SCRIPTS);
    untouched.document.getElementById("invoice-edit-btn").click();
    submit(untouched.document, "invoice-meta-form");
    await tick();
    expect(untouched.fetchMock.mock.calls[0][0]).toBe("/api/invoices/7");
    expect(untouched.fetchMock.mock.calls[0][1].method).toBe("PATCH");
    expect(untouched.fetchMock.mock.calls[0][1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(untouched.fetchMock).notes).toBeNull();

    const cleared = loadPage(detailFixture({ notes: "rush" }), SCRIPTS);
    cleared.document.getElementById("invoice-edit-btn").click();
    cleared.document.getElementById("invoice-meta-form").notes.value = "";
    submit(cleared.document, "invoice-meta-form");
    await tick();
    expect(fetchBody(cleared.fetchMock).notes).toBe("");
  });
});

describe("invoices.js — edit line", () => {
  it("sends null for untouched SPN but an explicit '' when cleared", async () => {
    const untouched = loadPage(detailFixture({ lineSpn: "SPN-9" }), SCRIPTS);
    untouched.document.querySelector('[data-act="edit-line"]').click();
    submit(untouched.document, "invoice-line-form");
    await tick();
    expect(untouched.fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/lines/3");
    expect(untouched.fetchMock.mock.calls[0][1].method).toBe("PUT");
    expect(untouched.fetchMock.mock.calls[0][1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(untouched.fetchMock).supplier_part_number).toBeNull();

    const cleared = loadPage(detailFixture({ lineSpn: "SPN-9" }), SCRIPTS);
    cleared.document.querySelector('[data-act="edit-line"]').click();
    cleared.document.getElementById("invoice-line-form").supplier_part_number.value =
      "";
    submit(cleared.document, "invoice-line-form");
    await tick();
    expect(fetchBody(cleared.fetchMock).supplier_part_number).toBe("");
  });

  it("rejects clearing a set location instead of silently ignoring it", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
    );
    document.querySelector('[data-act="edit-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "";
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    const error = document.getElementById("invoice-line-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/can't be cleared/);
  });

  it("rejects clearing a location picked via the — none — node", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
    );
    document.querySelector('[data-act="edit-line"]').click(); // prefilled to D1 (5)
    // Clear through the widget, not by writing the hidden input directly.
    document.querySelector(".loc-picker-none").click();
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    const error = document.getElementById("invoice-line-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/can't be cleared/);
  });

  it("applies a changed location through the location endpoint", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
    );
    document.querySelector('[data-act="edit-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "9";
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/lines/3");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/invoices/7/lines/3/location");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ location_id: 9 });
  });

  // The real call site for the inline flow: the New Location dialog opens on top
  // of the (already modal) line dialog, and its result selects into the picker.
  it("creates a location inline from the add-line picker and selects it", async () => {
    const fetchImpl = (url, opts) => {
      if (url === "/web/api/components") {
        return Promise.resolve({
          ok: true,
          json: async () => ({ data: [{ id: 11, mpn: "R-1", type: "resistor" }] }),
        });
      }
      if (url === "/api/locations" && opts?.method === "POST") {
        return Promise.resolve({
          ok: true,
          json: async () => ({ id: 12, name: "Bin 3", type: "box", parent_id: 5 }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const { document } = loadPage(
      detailFixture() + locationDialogFixture(),
      SCRIPTS_INLINE_LOCATION,
      { fetchImpl },
    );

    document.getElementById("invoice-addline-btn").click();
    await tick();
    document.querySelector("#invoice-line-dialog .loc-picker-toggle").click();
    document.querySelector("#invoice-line-dialog .loc-picker-new").click();
    document.querySelector('#location-form [name="name"]').value = "Bin 3";
    document.querySelector('#location-form [name="parent_id"]').value = "5";
    submit(document, "location-form");
    await tick();

    // Parent D1 (id 5) is in the picker, so the new location is pathed under it.
    expect(document.querySelector("#invoice-line-dialog [name='location_id']").value).toBe(
      "12",
    );
    expect(
      document.querySelector("#invoice-line-dialog .loc-picker-label").textContent.trim(),
    ).toBe("D1 / Bin 3");
  });
});

describe("invoices.js — remove line and finalize", () => {
  it("deletes a line after confirmation, with the CSRF header", async () => {
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    document.querySelector('[data-act="remove-line"]').click();
    await tick();

    expect(window.confirm).toHaveBeenCalled();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7/lines/3");
    expect(opts.method).toBe("DELETE");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("does not delete when the confirm is dismissed", async () => {
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    window.confirm.mockReturnValue(false);
    document.querySelector('[data-act="remove-line"]').click();
    await tick();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("deletes the draft after confirmation and returns to the list", async () => {
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    document.getElementById("invoice-delete-btn").click();
    await tick();

    expect(window.confirm).toHaveBeenCalled();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/invoices/7");
    expect(opts.method).toBe("DELETE");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("does not delete the draft when the confirm is dismissed", async () => {
    const { window, document, fetchMock } = loadPage(detailFixture(), SCRIPTS);
    window.confirm.mockReturnValue(false);
    document.getElementById("invoice-delete-btn").click();
    await tick();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("finalize sends a null gross when the field is blank", async () => {
    const { document, fetchMock } = loadPage(
      detailFixture({ withFinalize: true }),
      SCRIPTS,
    );
    document.getElementById("invoice-finalize-btn").click();
    submit(document, "invoice-finalize-form");
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/invoices/7/finalize");
    expect(fetchBody(fetchMock).total_gross).toBeNull();
  });
});

describe("invoices.js — error surfacing and add-line", () => {
  it("shows the server error message when a write fails", async () => {
    const { document, fetchMock } = loadPage(newInvoiceFixture(), SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          json: async () => ({ detail: "invoice 'INV-1' already exists" }),
        }),
    });
    const form = document.getElementById("invoice-new-form");
    form.supplier.value = "Mouser";
    form.invoice_number.value = "INV-1";
    form.invoice_date.value = "2026-07-08";
    form.currency.value = "EUR";
    form.notes.value = "";

    submit(document, "invoice-new-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const error = document.getElementById("invoice-new-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("invoice 'INV-1' already exists");
  });

  it("reports a partial failure when only the location step fails", async () => {
    // The line PUT succeeds; the follow-up location PUT fails.
    const fetchImpl = (url) =>
      Promise.resolve({
        ok: !url.endsWith("/location"),
        json: async () => ({ detail: "location was removed" }),
      });
    const { document, fetchMock } = loadPage(
      detailFixture({ lineLocationId: "5" }),
      SCRIPTS,
      { fetchImpl },
    );
    document.querySelector('[data-act="edit-line"]').click();
    document.getElementById("invoice-line-form").location_id.value = "9";
    submit(document, "invoice-line-form");
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const error = document.getElementById("invoice-line-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/location could not be set/);
    expect(error.textContent).toMatch(/location was removed/);
  });

  it("populates the component picker and posts a new line", async () => {
    const fetchImpl = (url) =>
      url === "/web/api/components"
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              data: [
                { id: 11, mpn: "R-1", manufacturer: "Yageo", type: "resistor" },
              ],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({ id: 99 }) });

    const { document, fetchMock } = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("invoice-addline-btn").click();
    await tick(); // let loadComponentOptions resolve and fill the <select>

    const select = document.getElementById("invoice-line-form").component_id;
    expect([...select.options].map((o) => o.value)).toEqual(["11"]);
    expect(select.value).toBe("11");

    const form = document.getElementById("invoice-line-form");
    form.quantity.value = "3";
    form.unit_price.value = "1.50";
    form.supplier_part_number.value = "";
    form.location_id.value = "";
    submit(document, "invoice-line-form");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/invoices/7/lines" && opts.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse(post[1].body)).toEqual({
      component_id: 11,
      quantity: 3,
      unit_price: "1.50",
      supplier_part_number: null,
      location_id: null,
    });
  });

  it("picks a location for the new line through the tree-picker", async () => {
    const fetchImpl = (url) =>
      url === "/web/api/components"
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              data: [{ id: 11, mpn: "R-1", manufacturer: null, type: "resistor" }],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({ id: 99 }) });
    const { document, fetchMock } = loadPage(detailFixture(), SCRIPTS, {
      fetchImpl,
    });
    document.getElementById("invoice-addline-btn").click();
    await tick();

    const form = document.getElementById("invoice-line-form");
    form.quantity.value = "2";
    form.unit_price.value = "1";
    // Select a location through the widget (node D1 = id 5).
    document.querySelector('.loc-picker-node[data-loc-id="5"]').click();
    expect(form.location_id.value).toBe("5");
    submit(document, "invoice-line-form");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/invoices/7/lines" && opts.method === "POST",
    );
    expect(JSON.parse(post[1].body).location_id).toBe(5);
  });

  it("hides the Component field on edit under the real app.css", () => {
    // openEditLine sets `componentField.hidden = true`, but `.field { display:flex }`
    // beats the UA `[hidden]` rule — without `.field[hidden] { display:none }` the
    // field stays on screen as an empty, unusable combobox. jsdom in loadPage has no
    // stylesheet, so only a computed-style check against the real CSS catches this.
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(
      `<style>${css}</style>
       <div class="field" id="cf" hidden><label>Component</label>
         <select><option>x</option></select></div>`,
    );
    const style = dom.window.getComputedStyle(dom.window.document.getElementById("cf"));
    expect(style.display).toBe("none");
  });

  it("really hides the error row under the real app.css", () => {
    // `.error-row { display: flex }` beats the UA `[hidden] { display: none }`, so
    // without restoring the attribute's authority the row stays on screen with a
    // stale message while every `.hidden` assertion still passes. Found in a
    // browser, not by the suite — which is the point of this check.
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(
      `<style>${css}</style>
       <p class="error-row" id="row" hidden><span class="error">boom</span>
         <button class="btn btn-secondary btn-sm">Retry</button></p>`,
    );
    const style = dom.window.getComputedStyle(dom.window.document.getElementById("row"));
    expect(style.display).toBe("none");
  });

  it("edit line reflects the existing location in the picker", () => {
    const { document } = loadPage(detailFixture({ lineLocationId: "5" }), SCRIPTS);
    document.querySelector('[data-act="edit-line"]').click();
    // openEditLine calls the picker's setValue -> hidden input and label follow.
    expect(document.querySelector('[name="location_id"]').value).toBe("5");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "D1",
    );
  });

  it("does not leak a location between lines when switching edits", () => {
    const { document } = loadPage(
      detailFixture({ lineLocationId: "5", secondLine: true }),
      SCRIPTS,
    );
    const editButtons = document.querySelectorAll('[data-act="edit-line"]');
    editButtons[0].click(); // line 3 has D1
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "D1",
    );
    editButtons[1].click(); // line 4 has no location -> reset() clears the prior pick
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "— none —",
    );
  });

  it("restores the placeholder when the add-line dialog is reopened", async () => {
    const fetchImpl = (url) =>
      url === "/web/api/components"
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              data: [{ id: 11, mpn: "R", manufacturer: null, type: "resistor" }],
            }),
          })
        : Promise.resolve({ ok: true, json: async () => ({}) });
    const { document } = loadPage(detailFixture(), SCRIPTS, { fetchImpl });

    document.getElementById("invoice-addline-btn").click();
    await tick();
    document.querySelector('.loc-picker-node[data-loc-id="5"]').click(); // pick D1
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "D1",
    );

    document.getElementById("invoice-addline-btn").click(); // reopen -> reset
    await tick();
    expect(document.querySelector('[name="location_id"]').value).toBe("");
    expect(document.querySelector(".loc-picker-label").textContent.trim()).toBe(
      "— none —",
    );
  });

  it("guards against a double submit while a write is in flight", async () => {
    const { document, fetchMock } = loadPage(newInvoiceFixture(), SCRIPTS, {
      fetchImpl: () =>
        new Promise((resolve) =>
          setTimeout(
            () => resolve({ ok: true, json: async () => ({ id: 1 }) }),
            15,
          ),
        ),
    });
    const form = document.getElementById("invoice-new-form");
    form.supplier.value = "Mouser";
    form.invoice_number.value = "INV-1";
    form.invoice_date.value = "2026-07-08";
    form.currency.value = "EUR";
    form.notes.value = "";

    submit(document, "invoice-new-form");
    submit(document, "invoice-new-form"); // second click while the first is in flight
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("invoices.js — new component from the add-line flow", () => {
  const SHARED = [
    "shared.js",
    "component_dialog.js",
    "location_tree.js",
    "invoices.js",
  ];

  it("creates a component in the picker and selects it, without new backend", async () => {
    // The picker feed deliberately never includes the new component, proving the
    // new option is inserted directly from the create response, not via a reload.
    const fetchImpl = (url, opts) => {
      if (url === "/web/api/components") {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            columns: [],
            data: [{ id: 5, mpn: "OLD", manufacturer: null, type: "resistor" }],
          }),
        });
      }
      if (url.startsWith("/api/types/") && url.endsWith("/parameters")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      if (url === "/api/components" && opts?.method === "POST") {
        return Promise.resolve({
          ok: true,
          json: async () => ({ id: 99, type_id: 1, mpn: "NEW", manufacturer: "Acme" }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };

    const { document } = loadPage(
      detailFixture() + componentDialogFixture(),
      SHARED,
      { fetchImpl },
    );

    // Per-instance close spies so we can prove only the inner dialog closes.
    const lineClose = vi.fn();
    const componentClose = vi.fn();
    document.getElementById("invoice-line-dialog").close = lineClose;
    document.getElementById("component-dialog").close = componentClose;

    document.getElementById("invoice-addline-btn").click();
    await tick(); // picker populated with the existing component (#5)

    document.getElementById("invoice-add-component-btn").click(); // open shared dialog
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "1";
    typeSelect.dispatchEvent(
      new document.defaultView.Event("change", { bubbles: true }),
    );
    await tick();
    submit(document, "component-form"); // create the component
    await tick();

    // The new component is now an option in the line picker, and selected,
    // even though the reload feed never contained it.
    const picker = document.querySelector('[name="component_id"]');
    expect([...picker.options].map((o) => o.value)).toContain("99");
    expect(picker.value).toBe("99");
    const newOption = picker.querySelector('option[value="99"]');
    expect(newOption.textContent).toBe("NEW · Acme");
    // Only the stacked component dialog closes; the line dialog stays open.
    expect(componentClose).toHaveBeenCalled();
    expect(lineClose).not.toHaveBeenCalled();
  });
});

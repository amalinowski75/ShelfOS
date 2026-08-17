import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, it, expect } from "vitest";
import {
  loadPage,
  tick,
  CSRF,
  componentPageFixture,
  componentDialogFixture,
} from "./harness.js";

// The dialog logic lives in component_dialog.js; app.js only wires the button.
const SCRIPTS = ["shared.js", "component_dialog.js", "type_dialog.js", "app.js"];

const DEFS = [
  { id: 10, label: "Resistance", data_type: "number", unit: "Ω", enum_values: [] },
  { id: 11, label: "Package", data_type: "text", unit: null, enum_values: [] },
  { id: 12, label: "RoHS", data_type: "bool", unit: null, enum_values: [] },
  {
    id: 13,
    label: "Dielectric",
    data_type: "enum",
    unit: null,
    enum_values: ["X7R", "C0G"],
  },
];

// Routes the endpoints the New Component flow touches.
function fetchImpl(url, opts) {
  if (url.startsWith("/api/types/") && url.endsWith("/parameters")) {
    return Promise.resolve({ ok: true, json: async () => DEFS });
  }
  if (url.startsWith("/web/api/components")) {
    return Promise.resolve({
      ok: true,
      json: async () => ({ columns: [], data: [] }),
    });
  }
  if (url === "/api/components" && opts?.method === "POST") {
    return Promise.resolve({ ok: true, json: async () => ({ id: 99, type_id: 1 }) });
  }
  return Promise.resolve({ ok: true, json: async () => ({}) });
}

function fire(el, type) {
  el.dispatchEvent(
    new el.ownerDocument.defaultView.Event(type, { cancelable: true, bubbles: true }),
  );
}

// Open the dialog and pick a type so its parameter fields render.
async function openWithType(impl = fetchImpl) {
  const handles = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
  handles.document.getElementById("new-component-btn").click();
  const typeSelect = handles.document.getElementById("component-type");
  typeSelect.value = "1";
  fire(typeSelect, "change");
  await tick(); // let the effective-parameters fetch resolve
  return handles;
}

describe("app.js — new component", () => {
  it("renders one value field per effective parameter", async () => {
    const { document } = await openWithType();
    const inputs = document.querySelectorAll(
      "#component-params [data-definition-id]",
    );
    expect(inputs.length).toBe(4);
    // enum -> select with the allowed tokens (plus a blank); bool -> yes/no.
    const enumSelect = document.querySelector('[data-definition-id="13"]');
    expect([...enumSelect.options].map((o) => o.value)).toEqual(["", "X7R", "C0G"]);
    const boolSelect = document.querySelector('[data-definition-id="12"]');
    expect([...boolSelect.options].map((o) => o.value)).toEqual(["", "true", "false"]);
  });

  it("posts only filled parameters: bool as boolean, number as its raw string", async () => {
    const { document, fetchMock } = await openWithType();
    document.getElementById("component-form").mpn.value = "R-100";
    document.querySelector('[data-definition-id="10"]').value = "4k7"; // number
    document.querySelector('[data-definition-id="12"]').value = "true"; // bool
    // Leave the text (11) and enum (13) fields empty — they must be skipped.
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components" && opts.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(post[1].headers["X-CSRF-Token"]).toBe(CSRF);
    const payload = JSON.parse(post[1].body);
    expect(payload.type_id).toBe(1);
    expect(payload.mpn).toBe("R-100");
    expect(payload.manufacturer).toBeNull();
    expect(payload.parameters).toEqual([
      { parameter_definition_id: 10, value: "4k7" },
      { parameter_definition_id: 12, value: true },
    ]);
  });

  it("surfaces the server error when create fails", async () => {
    const failImpl = (url, opts) =>
      url === "/api/components" && opts?.method === "POST"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "duplicate" }) })
        : fetchImpl(url, opts);
    const { document } = await openWithType(failImpl);
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const error = document.getElementById("component-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("duplicate");
    expect(error.querySelector("a")).toBeNull(); // a plain error has no link
  });

  it("links to the existing component when create is a duplicate", async () => {
    const dupImpl = (url, opts) =>
      url === "/api/components" && opts?.method === "POST"
        ? Promise.resolve({
            ok: false,
            json: async () => ({
              detail: "A component with MPN R-100 from YAGEO already exists.",
              existing_id: 42,
            }),
          })
        : fetchImpl(url, opts);
    const { document } = await openWithType(dupImpl);
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const error = document.getElementById("component-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("already exists");
    const link = error.querySelector("a");
    expect(link).toBeTruthy();
    expect(link.getAttribute("href")).toBe("/components/42");
  });

  it("ignores a second submit while the create is in flight", async () => {
    let resolveFetch;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    const impl = (url, opts) =>
      url === "/api/components" && opts?.method === "POST"
        ? pending
        : fetchImpl(url, opts);
    const { document, fetchMock } = await openWithType(impl);
    document.getElementById("component-form").mpn.value = "R-100";
    fire(document.getElementById("component-form"), "submit"); // POST in flight
    fire(document.getElementById("component-form"), "submit"); // must be ignored
    const posts = fetchMock.mock.calls.filter(
      ([url, opts]) => url === "/api/components" && opts.method === "POST",
    );
    expect(posts.length).toBe(1);
    resolveFetch({ ok: true, json: async () => ({ id: 99, type_id: 1 }) });
    await tick();
  });

  it("keeps explicit falsy values (bool 'no', number '0')", async () => {
    const { document, fetchMock } = await openWithType();
    document.querySelector('[data-definition-id="10"]').value = "0"; // number zero
    document.querySelector('[data-definition-id="12"]').value = "false"; // bool no
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const post = fetchMock.mock.calls.find(
      ([url, opts]) => url === "/api/components" && opts.method === "POST",
    );
    expect(JSON.parse(post[1].body).parameters).toEqual([
      { parameter_definition_id: 10, value: "0" },
      { parameter_definition_id: 12, value: false },
    ]);
  });

  it("ignores a stale slow response for a superseded type", async () => {
    const A = { id: 20, label: "A", data_type: "text", unit: null, enum_values: [] };
    const B = { id: 21, label: "B", data_type: "text", unit: null, enum_values: [] };
    const raced = (url) => {
      if (url === "/api/types/1/parameters") {
        return new Promise((resolve) =>
          setTimeout(() => resolve({ ok: true, json: async () => [A] }), 30),
        );
      }
      if (url === "/api/types/2/parameters") {
        return Promise.resolve({ ok: true, json: async () => [B] });
      }
      return fetchImpl(url);
    };
    const { document } = loadPage(
      componentPageFixture([
        { id: 1, name: "r" },
        { id: 2, name: "c" },
      ]),
      SCRIPTS,
      { fetchImpl: raced },
    );
    document.getElementById("new-component-btn").click();
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "1";
    fire(typeSelect, "change"); // slow
    typeSelect.value = "2";
    fire(typeSelect, "change"); // fast — supersedes the first
    await new Promise((resolve) => setTimeout(resolve, 60)); // let both settle

    const ids = [
      ...document.querySelectorAll("#component-params [data-definition-id]"),
    ].map((i) => i.dataset.definitionId);
    expect(ids).toEqual(["21"]); // only the newer type's field, not the stale one
  });

  it("clears the parameter fields when the dialog is reopened", async () => {
    const { document } = await openWithType();
    expect(
      document.querySelectorAll("#component-params [data-definition-id]").length,
    ).toBe(4);

    document.getElementById("new-component-btn").click(); // reopen
    expect(
      document.querySelectorAll("#component-params [data-definition-id]").length,
    ).toBe(0);
    expect(document.getElementById("component-type").value).toBe("");
  });

  it("adds a type via the New Type dialog and selects it in the component dialog", async () => {
    // The new type (id 7) has its own parameter definition, so we can assert the
    // component dialog rendered its fields — not just that it was selected.
    const impl = (url, opts) => {
      if (url === "/api/types" && opts?.method === "POST") {
        return Promise.resolve({ ok: true, json: async () => ({ id: 7, name: "capacitor" }) });
      }
      if (url === "/api/types/7/parameters") {
        return Promise.resolve({
          ok: true,
          json: async () => [
            { id: 30, label: "Capacitance", data_type: "number", unit: "F", enum_values: [] },
          ],
        });
      }
      return fetchImpl(url, opts);
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });

    document.getElementById("new-component-btn").click();
    const newTypeBtn = document.getElementById("component-new-type");
    expect(newTypeBtn.hidden).toBe(false); // the type builder is on this page
    newTypeBtn.click(); // opens the New Type dialog (stacked)

    const typeForm = document.getElementById("type-form");
    typeForm.querySelector('[name="type-name"]').value = "capacitor";
    fire(typeForm, "submit");
    await tick();

    const select = document.getElementById("component-type");
    const option = [...select.options].find((o) => o.value === "7");
    expect(option).toBeTruthy();
    expect(option.textContent).toBe("capacitor");
    expect(select.value).toBe("7"); // the new type is selected…
    // …and its parameter fields are loaded, ready to fill.
    expect(
      document.querySelector('#component-params [data-definition-id="30"]'),
    ).toBeTruthy();
  });

  it("hides + New type where the page has no type builder", () => {
    // The invoice add-line reuse: the component dialog is present, the New Type
    // dialog is not, so the JS leaves the button's hidden attribute in place and
    // wires no handler.
    const { document } = loadPage(componentDialogFixture(), [
      "shared.js",
      "component_dialog.js",
    ]);
    expect(document.getElementById("component-new-type").hidden).toBe(true);
  });

  it("a hidden .btn is actually not displayed under the real app.css", () => {
    // The attribute alone isn't enough: .btn sets `display`, which overrides the
    // UA [hidden] rule, so a real .btn[hidden] rule is needed. Assert computed
    // display against the real stylesheet — this fails if that rule is dropped.
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(
      `<style>${css}</style><button class="btn" id="x" hidden></button>`,
    );
    const btn = dom.window.document.getElementById("x");
    expect(dom.window.getComputedStyle(btn).display).toBe("none");
  });

  it("the .warn status class is a real, distinct colour in app.css", () => {
    // The label-only import styles itself "warn"; without a rule that would render
    // as ordinary body text and the partial-success signal would vanish silently.
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(
      `<style>${css}</style><p class="warn" id="w"></p><p class="error" id="e"></p>` +
        `<p id="p"></p>`,
    );
    const colour = (id) =>
      dom.window.getComputedStyle(dom.window.document.getElementById(id)).color;
    expect(colour("w")).toBeTruthy();
    expect(colour("w")).not.toBe(colour("e"));
    expect(colour("w")).not.toBe(colour("p"));
  });

  it("loads cleanly when the create controls are absent (read-only)", () => {
    // A page without #component-dialog / #new-component-btn must not throw at
    // load; the harness fails the test on any unhandled script error.
    const { document } = loadPage(
      `<select id="type-filter"></select>
       <div id="components-table"></div>
       <dialog id="stock-dialog"><form id="stock-form"></form></dialog>`,
      SCRIPTS,
    );
    expect(document.getElementById("new-component-btn")).toBeNull();
  });

  it("surfaces a message when the create request never reaches the server", async () => {
    const failImpl = (url, opts) =>
      url === "/api/components" && opts?.method === "POST"
        ? Promise.reject(new Error("network down"))
        : fetchImpl(url, opts);
    const { document } = await openWithType(failImpl);
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const error = document.getElementById("component-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/Could not reach the server/);
  });

  it("shows a loading placeholder while a type's parameters are fetched", () => {
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("new-component-btn").click();
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "1";
    fire(typeSelect, "change");
    // Synchronously, before the fetch resolves, the hint reads "Loading…".
    const hint = document.getElementById("component-params-hint");
    expect(hint.hidden).toBe(false);
    expect(hint.textContent).toBe("Loading…");
  });
});

describe("component_dialog.js — stage mode (invoice import review)", () => {
  it("prefills type + params from the staged row and PATCHes on save (no create)", async () => {
    const saved = [];
    const { window, document, fetchMock } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl,
    });

    window.openComponentDialog(
      (r) => saved.push(r),
      {
        typeId: 1,
        mpn: "R-1",
        manufacturer: "Acme",
        package: "0402",
        mountingType: "THT",
        notes: "a res",
        paramValues: [{ parameter_definition_id: 10, value: "4k7" }],
      },
      { stage: { invoiceId: 7, importLineId: 21 } },
    );
    await tick(); // params load + prefill

    // Type selected, the type's params rendered, the stored value + mounting applied.
    expect(document.getElementById("component-type").value).toBe("1");
    expect(document.querySelector('[data-definition-id="10"]').value).toBe("4k7");
    expect(document.getElementById("component-form").mpn.value).toBe("R-1");
    expect(
      document.getElementById("component-form").mounting_type.value,
    ).toBe("THT");

    fire(document.getElementById("component-form"), "submit");
    await tick();

    // Saved to the import line — NOT a component create.
    const patch = fetchMock.mock.calls.find(
      ([url, opts]) =>
        url === "/api/invoices/7/import-lines/21" && opts.method === "PATCH",
    );
    expect(patch).toBeTruthy();
    expect(patch[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(patch[1].body)).toEqual({
      type_id: 1,
      manufacturer: "Acme",
      mpn: "R-1",
      package: "0402",
      mounting_type: "THT",
      description: "a res",
      parameters: [{ parameter_definition_id: 10, value: "4k7" }],
    });
    expect(
      fetchMock.mock.calls.some(
        ([url, opts]) => url === "/api/components" && opts?.method === "POST",
      ),
    ).toBe(false);
    expect(saved).toHaveLength(1);
  });
});

describe("component_dialog.js — shop import", () => {
  const PRODUCT = {
    category: "resistor",
    mpn: "RES-10K",
    manufacturer: "YAGEO",
    description: "10k 1% 0402 resistor",
    package: "0402",
    datasheet_url: "https://x/ds.pdf",
    // The product page the SERVER resolved the code to — what gets saved as the
    // shop link. Echoed back because a scanned code buries (or lacks) its URL.
    source_url: "https://www.mouser.com/x",
    from_label_only: false,
    parameters: [{ name: "Resistance", value: "10 kOhms" }],
    // The matching engine now runs SERVER-side; the dialog just applies its proposal.
    proposal: {
      type_id: 1,
      mounting_type: "SMT",
      package: "0402",
      parameters: [
        { parameter_definition_id: 10, value: "10k" }, // NUMBER field
        { parameter_definition_id: 11, value: "1206 (3216 Metric)" }, // TEXT field
      ],
    },
  };
  const withLookup = (product) => (url, opts) =>
    url === "/api/shops/lookup"
      ? Promise.resolve({ ok: true, json: async () => product })
      : fetchImpl(url, opts);

  async function openAndImport(document) {
    document.getElementById("new-component-btn").click();
    document.getElementById("shop-import-url").value = "https://www.mouser.com/x";
    document.getElementById("shop-import-btn").click();
    await tick();
    await tick(); // lookup, then the type's parameters
  }

  it("applies the server proposal from a looked-up product", async () => {
    // The matching (type, mounting, parameter values) is computed server-side now;
    // the dialog applies the proposal it returns. The value-derivation logic itself
    // is covered by the Python engine tests (test_matching.py).
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(PRODUCT),
    });
    await openAndImport(document);
    expect(document.getElementById("component-type").value).toBe("1"); // resistor
    const field = (name) =>
      document.querySelector(`#component-form [name="${name}"]`).value;
    expect(field("mpn")).toBe("RES-10K");
    expect(field("manufacturer")).toBe("YAGEO");
    expect(field("package")).toBe("0402");
    expect(field("notes")).toBe("10k 1% 0402 resistor");
    expect(field("mounting_type")).toBe("SMT"); // from proposal.mounting_type
    // Parameter values come straight from proposal.parameters (by definition id).
    expect(
      document.querySelector('#component-params [data-definition-id="10"]').value,
    ).toBe("10k");
    expect(
      document.querySelector('#component-params [data-definition-id="11"]').value,
    ).toBe("1206 (3216 Metric)");
  });

  it("re-runs the engine for the newly chosen type when the type changes", async () => {
    let proposalBody = null;
    const impl = (url, opts) => {
      if (url === "/api/shops/lookup") {
        return Promise.resolve({ ok: true, json: async () => PRODUCT });
      }
      if (url === "/api/matching/proposal") {
        proposalBody = JSON.parse(opts.body);
        return Promise.resolve({
          ok: true,
          json: async () => ({
            type_id: 2,
            mounting_type: null,
            package: null,
            parameters: [{ parameter_definition_id: 10, value: "8k" }],
          }),
        });
      }
      return fetchImpl(url, opts);
    };
    const { document } = loadPage(
      componentPageFixture([
        { id: 1, name: "resistor" },
        { id: 2, name: "capacitor" },
      ]),
      SCRIPTS,
      { fetchImpl: impl },
    );
    await openAndImport(document);
    // Switch the type: the dialog re-requests a proposal for it and applies it.
    const select = document.getElementById("component-type");
    select.value = "2";
    fire(select, "change");
    await tick();
    await tick();
    // The re-request carries the imported product's text so the server can re-match.
    expect(proposalBody.type_id).toBe(2);
    expect(proposalBody.description).toBe("10k 1% 0402 resistor");
    expect(
      document.querySelector('#component-params [data-definition-id="10"]').value,
    ).toBe("8k");
  });

  it("keeps a manually-corrected mounting when the type changes", async () => {
    const impl = (url, opts) => {
      if (url === "/api/shops/lookup") {
        return Promise.resolve({ ok: true, json: async () => PRODUCT });
      }
      if (url === "/api/matching/proposal") {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            type_id: 2,
            mounting_type: "SMT", // the engine still guesses SMT for the new type
            package: null,
            parameters: [],
          }),
        });
      }
      return fetchImpl(url, opts);
    };
    const { document } = loadPage(
      componentPageFixture([
        { id: 1, name: "resistor" },
        { id: 2, name: "capacitor" },
      ]),
      SCRIPTS,
      { fetchImpl: impl },
    );
    await openAndImport(document);
    const mounting = document.querySelector(
      '#component-form [name="mounting_type"]',
    );
    expect(mounting.value).toBe("SMT"); // applied from the initial proposal
    mounting.value = "THT"; // the user corrects it by hand

    const select = document.getElementById("component-type");
    select.value = "2";
    fire(select, "change");
    await tick();
    await tick();
    // Mounting is type-independent; the re-fetch must not clobber the correction.
    expect(mounting.value).toBe("THT");
  });

  it("discards a stale proposal for a superseded type", async () => {
    const impl = (url, opts) => {
      if (url === "/api/shops/lookup") {
        return Promise.resolve({ ok: true, json: async () => PRODUCT });
      }
      if (url === "/api/matching/proposal") {
        const body = JSON.parse(opts.body);
        const value = body.type_id === 1 ? "99k" : "8k";
        const delay = body.type_id === 1 ? 30 : 0; // the superseded one is slow
        return new Promise((resolve) =>
          setTimeout(
            () =>
              resolve({
                ok: true,
                json: async () => ({
                  type_id: body.type_id,
                  mounting_type: null,
                  package: null,
                  parameters: [{ parameter_definition_id: 10, value }],
                }),
              }),
            delay,
          ),
        );
      }
      return fetchImpl(url, opts);
    };
    const { document } = loadPage(
      componentPageFixture([
        { id: 1, name: "resistor" },
        { id: 2, name: "capacitor" },
      ]),
      SCRIPTS,
      { fetchImpl: impl },
    );
    await openAndImport(document);
    const select = document.getElementById("component-type");
    select.value = "1";
    fire(select, "change"); // slow proposal (99k) — superseded
    select.value = "2";
    fire(select, "change"); // fast proposal (8k) — the live one
    await new Promise((resolve) => setTimeout(resolve, 60)); // let both settle
    // The stale type-1 response resolves last but must not overwrite the type-2 value.
    expect(
      document.querySelector('#component-params [data-definition-id="10"]').value,
    ).toBe("8k");
  });

  it("attaches the imported datasheet after the component is created", async () => {
    const { document, fetchMock } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(PRODUCT),
    });
    await openAndImport(document);
    fire(document.getElementById("component-form"), "submit");
    await tick();
    const attach = fetchMock.mock.calls.find(
      (c) => c[0] === "/api/attachments/from-url",
    );
    expect(attach).toBeTruthy();
    expect(JSON.parse(attach[1].body)).toMatchObject({
      entity_type: "component",
      entity_id: 99,
      url: "https://x/ds.pdf",
      kind: "datasheet",
    });
  });

  it("reads the mounting type from the shop's category, not just the description", async () => {
    // A real TME capacitor: the description never says SMD — only the shop's own
    // category does. Before shop_category reached the client this was lost.
    const tmeish = {
      category: "capacitor",
      shop_category: "MLCC SMD capacitors",
      mpn: "0603B104K500CT",
      manufacturer: "WALSIN",
      description: "Capacitor: ceramic; 100nF; 50V; X7R; ±10%; 0603",
      datasheet_url: null,
      parameters: [],
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(tmeish),
    });
    await openAndImport(document);
    expect(
      document.querySelector('#component-form [name="mounting_type"]').value,
    ).toBe("SMT");
  });

  it("reads a spelled-out surface mount category", async () => {
    // Digi-Key writes it out in full where TME abbreviates.
    const digikeyish = {
      category: "resistor",
      shop_category: "Chip Resistor - Surface Mount",
      mpn: "R-1",
      description: "RES 1.2K OHM 1%",
      parameters: [],
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(digikeyish),
    });
    await openAndImport(document);
    expect(
      document.querySelector('#component-form [name="mounting_type"]').value,
    ).toBe("SMT");
  });

  it("does not mine numbers out of the category into parameter fields", async () => {
    // The category joins the package/mounting scan only: its digits must never be
    // read as a measurement.
    const noisy = {
      category: "resistor",
      shop_category: "Resistors 100 Ohm series",
      mpn: "R-2",
      description: "Resistor without a stated value",
      parameters: [],
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(noisy),
    });
    await openAndImport(document);
    expect(
      document.querySelector('#component-params [data-definition-id="10"]').value,
    ).toBe("");
  });

  it("saves the shop URL the component was imported from as a link", async () => {
    const { document, fetchMock } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(PRODUCT),
    });
    await openAndImport(document); // pastes https://www.mouser.com/x
    fire(document.getElementById("component-form"), "submit");
    await tick();
    const link = fetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/links" && JSON.parse(c[1].body).kind === "shop",
    );
    expect(link).toBeTruthy();
    expect(JSON.parse(link[1].body)).toMatchObject({
      entity_type: "component",
      entity_id: 99,
      url: "https://www.mouser.com/x",
      kind: "shop",
    });
  });

  it("saves the datasheet as a link when it can't be downloaded, and says so", async () => {
    // TME's document host answers a server-side GET with a Cloudflare challenge, so
    // the download 422s. The datasheet must not be lost: it becomes a link instead.
    const impl = (url, opts) =>
      url === "/api/attachments/from-url"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "nope" }) })
        : withLookup(PRODUCT)(url, opts);
    const { document, fetchMock } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: impl,
    });
    await openAndImport(document);
    fire(document.getElementById("component-form"), "submit");
    await tick();

    const link = fetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/links" && JSON.parse(c[1].body).kind === "datasheet",
    );
    expect(link).toBeTruthy();
    expect(JSON.parse(link[1].body).url).toBe("https://x/ds.pdf");
    const toast = document.querySelector(".toast");
    expect(toast.textContent).toMatch(/saved as a link/);
    // The component itself still exists.
    expect(document.getElementById("component-dialog").open).toBe(false);
  });

  it("warns when the shop link can't be saved", async () => {
    // Only the shop-link POST fails; the datasheet still downloads.
    const impl = (url, opts) => {
      if (url === "/api/links" && JSON.parse(opts.body).kind === "shop") {
        return Promise.resolve({ ok: false, json: async () => ({ detail: "no" }) });
      }
      return withLookup(PRODUCT)(url, opts);
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
    await openAndImport(document);
    fire(document.getElementById("component-form"), "submit");
    await tick();
    const toast = document.querySelector(".toast");
    expect(toast.textContent).toMatch(/couldn't save the shop link/);
  });

  it("joins both losses with 'and', not 'or'", async () => {
    // Both the shop link and the datasheet are lost — the message must not read as
    // though one of them survived.
    const impl = (url, opts) => {
      if (url === "/api/links" || url === "/api/attachments/from-url") {
        return Promise.resolve({ ok: false, json: async () => ({ detail: "no" }) });
      }
      return withLookup(PRODUCT)(url, opts);
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
    await openAndImport(document);
    fire(document.getElementById("component-form"), "submit");
    await tick();
    expect(document.querySelector(".toast").textContent).toContain(
      "the shop link and the datasheet",
    );
  });

  it("warns when the datasheet can be neither downloaded nor linked", async () => {
    // Both the download and the link fallback fail — the datasheet is genuinely
    // lost, and that must be reported rather than swallowed.
    const impl = (url, opts) => {
      if (url === "/api/attachments/from-url" || url === "/api/links") {
        return Promise.resolve({ ok: false, json: async () => ({ detail: "no" }) });
      }
      return withLookup(PRODUCT)(url, opts);
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
    await openAndImport(document);
    fire(document.getElementById("component-form"), "submit");
    await tick();
    const toast = document.querySelector(".toast");
    expect(toast).toBeTruthy();
    expect(toast.textContent).toMatch(/couldn't save/);
    expect(toast.textContent).toMatch(/datasheet/);
  });

  it("shows no warning and adds no datasheet link when the datasheet downloads", async () => {
    const { document, fetchMock } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(PRODUCT),
    });
    await openAndImport(document);
    fire(document.getElementById("component-form"), "submit");
    await tick();
    expect(document.querySelector(".toast")).toBeNull();
    // The datasheet was downloaded as a file, so no datasheet LINK is created…
    const dsLink = fetchMock.mock.calls.find(
      (c) => c[0] === "/api/links" && JSON.parse(c[1].body).kind === "datasheet",
    );
    expect(dsLink).toBeFalsy();
    // …but the shop link is still saved.
    expect(
      fetchMock.mock.calls.some(
        (c) => c[0] === "/api/links" && JSON.parse(c[1].body).kind === "shop",
      ),
    ).toBe(true);
  });

  it("shows an error when the lookup fails", async () => {
    const impl = (url, opts) =>
      url === "/api/shops/lookup"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "unsupported shop" }) })
        : fetchImpl(url, opts);
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("new-component-btn").click();
    document.getElementById("shop-import-url").value = "https://x";
    document.getElementById("shop-import-btn").click();
    await tick();
    const status = document.getElementById("shop-import-status");
    expect(status.hidden).toBe(false);
    expect(status.textContent).toBe("unsupported shop");
    expect(status.className).toBe("error");
  });

  it("focuses the import field on open so a scan lands there", () => {
    // A keyboard-wedge scanner types into whatever has focus; without this the
    // payload would be lost (or land in the first form field).
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("new-component-btn").click();
    expect(document.activeElement.id).toBe("shop-import-url");
  });

  it("imports on Enter, so a scan needs no click", async () => {
    // Scanners end their payload with Enter. It must import, not submit the form.
    const { document, fetchMock } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(PRODUCT),
    });
    document.getElementById("new-component-btn").click();
    const field = document.getElementById("shop-import-url");
    field.value = "https://www.mouser.com/x";
    field.dispatchEvent(
      new document.defaultView.KeyboardEvent("keydown", {
        key: "Enter",
        cancelable: true,
        bubbles: true,
      }),
    );
    await tick();
    await tick();
    expect(document.querySelector('#component-form [name="mpn"]').value).toBe(
      "RES-10K",
    );
    // The endpoint takes a URL *or* a scan, so the field is `code`, not `url`.
    const lookup = fetchMock.mock.calls.find((c) => c[0] === "/api/shops/lookup");
    expect(JSON.parse(lookup[1].body)).toEqual({ code: "https://www.mouser.com/x" });
    // The form was never submitted by that Enter.
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/components")).toBe(false);
  });

  // The shop link comes from the server's `source_url`, never from the raw code:
  // a barcode has no URL at all, and a TME QR buries one among other tokens — so
  // testing the code itself would both store junk and drop a real link.
  async function importAndSubmit(product, code) {
    const handles = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(product),
    });
    handles.document.getElementById("new-component-btn").click();
    handles.document.getElementById("shop-import-url").value = code;
    handles.document.getElementById("shop-import-btn").click();
    await tick();
    await tick();
    fire(handles.document.getElementById("component-form"), "submit");
    await tick();
    return handles.fetchMock.mock.calls.find(
      (c) => c[0] === "/api/links" && JSON.parse(c[1].body).kind === "shop",
    );
  }

  it("does not save a scanned barcode as the shop link", async () => {
    const link = await importAndSubmit(
      { ...PRODUCT, datasheet_url: null, source_url: null },
      "\x1d1P5277\x1d1VKeystone",
    );
    expect(link).toBeFalsy();
  });

  it("saves the URL a scanned QR resolved to, not the whole payload", async () => {
    const url = "https://www.tme.eu/details/MIC334";
    const link = await importAndSubmit(
      { ...PRODUCT, datasheet_url: null, source_url: url },
      `QTY:5 PN:MIC334 RoHS ${url}`,
    );
    expect(JSON.parse(link[1].body).url).toBe(url);
  });

  it("says so when a second scan arrives mid-lookup", async () => {
    // Two labels scanned in quick succession is the normal wedge-scanner mishap;
    // dropping the second silently leaves the user unsure which one landed.
    let release;
    const impl = (url, opts) =>
      url === "/api/shops/lookup"
        ? new Promise((resolve) => {
            release = () => resolve({ ok: true, json: async () => PRODUCT });
          })
        : fetchImpl(url, opts);
    const { document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("new-component-btn").click();
    const field = document.getElementById("shop-import-url");
    field.value = "https://www.mouser.com/x";
    document.getElementById("shop-import-btn").click();
    document.getElementById("shop-import-btn").click(); // the second scan
    const status = document.getElementById("shop-import-status");
    expect(status.textContent).toMatch(/previous code/);
    expect(status.className).toBe("error");
    release();
    await tick();
    await tick();
    // …and the first lookup still lands.
    expect(document.querySelector('#component-form [name="mpn"]').value).toBe(
      "RES-10K",
    );
  });

  it("says so when only the scanned label could be read", async () => {
    // An unconfigured/failing shop still yields the label's MPN + manufacturer —
    // but the user must not read that as a full import.
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup({
        category: null,
        shop_category: null,
        mpn: "5277",
        manufacturer: "Keystone",
        description: null,
        package: null,
        datasheet_url: null,
        source_url: null,
        from_label_only: true,
        parameters: [],
      }),
    });
    await openAndImport(document);
    const field = (name) =>
      document.querySelector(`#component-form [name="${name}"]`).value;
    expect(field("mpn")).toBe("5277");
    expect(field("manufacturer")).toBe("Keystone");
    const status = document.getElementById("shop-import-status");
    expect(status.textContent).toMatch(/label only/);
    // Not "error": the fields ARE filled, so red would overstate the failure.
    expect(status.className).toBe("warn");
  });
});

describe("component_dialog.js — prefill (add from BOM)", () => {
  it("matches the type by category name and fills value/mpn/manufacturer", async () => {
    const { window, document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl });
    // Case-insensitive match to the "resistor" option (id 1 in the fixture).
    window.openComponentDialog(null, {
      category: "Resistor",
      value: "10k 1%",
      mpn: "RES-10K",
      manufacturer: "YAGEO",
    });
    await tick();
    expect(document.getElementById("component-type").value).toBe("1");
    expect(document.querySelector('#component-form [name="mpn"]').value).toBe("RES-10K");
    expect(document.querySelector('#component-form [name="manufacturer"]').value).toBe(
      "YAGEO",
    );
    // The value lands in the type's first NUMBER field, suffix stripped.
    const numberInput = document.querySelector(
      '#component-params input[data-data-type="number"]',
    );
    expect(numberInput.value).toBe("10k");
  });

  it("leaves the type unselected when no name matches", async () => {
    const { window, document } = loadPage(componentPageFixture(), SCRIPTS, { fetchImpl });
    window.openComponentDialog(null, { category: "transistor", mpn: "BC847" });
    await tick();
    expect(document.getElementById("component-type").value).toBe("");
    expect(document.querySelector('#component-form [name="mpn"]').value).toBe("BC847");
    expect(document.getElementById("component-params-hint").hidden).toBe(false);
  });

  it("fills the value parameter by (sort_order, id), not DOM order", async () => {
    // The value param must be the lowest-order NUMBER (like the server), even when
    // an inherited number renders first in the list.
    const inheritDefs = [
      { id: 20, label: "Base", data_type: "number", unit: null, enum_values: [], sort_order: 5 },
      { id: 21, label: "Resistance", data_type: "number", unit: "Ω", enum_values: [], sort_order: 1 },
    ];
    const impl = (url, opts) =>
      url.startsWith("/api/types/") && url.endsWith("/parameters")
        ? Promise.resolve({ ok: true, json: async () => inheritDefs })
        : fetchImpl(url, opts);
    const { window, document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: impl,
    });
    window.openComponentDialog(null, { category: "resistor", value: "10k" });
    await tick();
    expect(
      document.querySelector('#component-params input[data-definition-id="21"]').value,
    ).toBe("10k");
    expect(
      document.querySelector('#component-params input[data-definition-id="20"]').value,
    ).toBe("");
  });

  it("does not fill the value if the type is changed while parameters load", async () => {
    const { window, document } = loadPage(
      componentPageFixture([
        { id: 1, name: "resistor" },
        { id: 2, name: "capacitor" },
      ]),
      SCRIPTS,
      { fetchImpl },
    );
    window.openComponentDialog(null, { category: "resistor", value: "10k" });
    // Before the prefill's params load, the user switches type.
    const typeSelect = document.getElementById("component-type");
    typeSelect.value = "2";
    fire(typeSelect, "change");
    await tick();
    // The prefill value must NOT land in the now-current (different) type's field.
    expect(
      document.querySelector('#component-params input[data-data-type="number"]').value,
    ).toBe("");
  });
});

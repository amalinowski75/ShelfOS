import { describe, it, expect } from "vitest";
import {
  loadPage,
  tick,
  CSRF,
  componentPageFixture,
  componentDialogFixture,
} from "./harness.js";

// The dialog logic lives in component_dialog.js; app.js only wires the button.
const SCRIPTS = ["shared.js", "component_dialog.js", "app.js"];

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
    // dialog is not, so the button must stay hidden.
    const { document } = loadPage(componentDialogFixture(), [
      "shared.js",
      "component_dialog.js",
    ]);
    expect(document.getElementById("component-new-type").hidden).toBe(true);
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

describe("component_dialog.js — shop import", () => {
  const PRODUCT = {
    category: "resistor",
    mpn: "RES-10K",
    manufacturer: "YAGEO",
    description: "10k 1% 0402 resistor",
    package: "0402",
    datasheet_url: "https://x/ds.pdf",
    parameters: [
      { name: "Resistance", value: "10 kOhms" }, // NUMBER field → cleaned to "10k"
      { name: "Package", value: "1206 (3216 Metric)" }, // TEXT field → kept raw
      { name: "Tolerance", value: "1" }, // no matching def in DEFS → dropped
    ],
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

  it("rich-prefills the dialog from a looked-up product", async () => {
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
    // "Resistance" → the NUMBER field (DEFS id 10), engineering-cleaned.
    expect(
      document.querySelector('#component-params [data-definition-id="10"]').value,
    ).toBe("10k");
    // "Package" → the TEXT field (DEFS id 11), kept raw (not truncated).
    expect(
      document.querySelector('#component-params [data-definition-id="11"]').value,
    ).toBe("1206 (3216 Metric)");
  });

  it("derives fields from the description when the API returns no specs", async () => {
    // Mirrors a real Mouser response: ProductAttributes carry only logistics, so
    // the specs must come out of the description.
    const mouserish = {
      category: "resistor",
      mpn: "MR04X1201FTL",
      manufacturer: "Walsin",
      description:
        "Thick Film Resistors - SMD 1.2 kOhms 50 V 100 mW 1 % 0402 100 PPM / C AEC-Q200",
      datasheet_url: null,
      parameters: [
        { name: "Packaging", value: "Reel" },
        { name: "Standard Pack Qty", value: "10000" },
      ],
    };
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookup(mouserish),
    });
    await openAndImport(document);
    // Resistance (unit Ω) read from the description; the stray "50 V" is ignored
    // because this type has no volt parameter.
    expect(
      document.querySelector('#component-params [data-definition-id="10"]').value,
    ).toBe("1.2k");
    expect(document.querySelector('#component-form [name="package"]').value).toBe(
      "0402",
    );
    expect(
      document.querySelector('#component-form [name="mounting_type"]').value,
    ).toBe("SMT");
  });

  // A resistor type with both a Ω and a W parameter, to exercise the unit scan.
  const RES_DEFS = [
    { id: 10, label: "Resistance", data_type: "number", unit: "Ω", enum_values: [], sort_order: 0 },
    { id: 14, label: "Power", data_type: "number", unit: "W", enum_values: [], sort_order: 2 },
  ];
  const withLookupAndResDefs = (product) => (url, opts) => {
    if (url === "/api/shops/lookup") {
      return Promise.resolve({ ok: true, json: async () => product });
    }
    if (url.startsWith("/api/types/") && url.endsWith("/parameters")) {
      return Promise.resolve({ ok: true, json: async () => RES_DEFS });
    }
    return fetchImpl(url, opts);
  };
  const param = (document, id) =>
    document.querySelector(`#component-params [data-definition-id="${id}"]`).value;

  it("reads a fractional power rating (1/16W), not its denominator", async () => {
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookupAndResDefs({
        category: "resistor",
        description: "Thick Film Resistors 1.2kOhms 1/16W 0402 5% AEC-Q200",
        parameters: [],
      }),
    });
    await openAndImport(document);
    expect(param(document, 10)).toBe("1.2k");
    expect(param(document, 14)).toBe("0.0625"); // 1/16 W, not 16 W
  });

  it("reads a unitless engineering value into the value parameter", async () => {
    const { document } = loadPage(componentPageFixture(), SCRIPTS, {
      fetchImpl: withLookupAndResDefs({
        category: "resistor",
        description: "Thin Film Resistors - SMD TNPW-0402 1.2K 0.1% T-9 RT7",
        parameters: [],
      }),
    });
    await openAndImport(document);
    // "1.2K" carries no unit but is plainly the resistance…
    expect(param(document, 10)).toBe("1.2k");
    // …and the package code must not be mistaken for it.
    expect(document.querySelector('#component-form [name="package"]').value).toBe(
      "0402",
    );
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

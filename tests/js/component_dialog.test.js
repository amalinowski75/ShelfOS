import { describe, it, expect, vi } from "vitest";
import { loadPage, tick } from "./harness.js";

const SCRIPTS = ["shared.js", "component_dialog.js"];

const ok = (data) => Promise.resolve({ ok: true, json: () => Promise.resolve(data) });

// The New Component dialog, with the shop-import field and the "Open in shop"
// footer button this suite exercises.
function dialogFixture() {
  return `
    <dialog id="component-dialog"><form id="component-form">
      <input id="shop-import-url" type="text" />
      <button type="button" id="shop-import-btn"></button>
      <p id="shop-import-status" hidden></p>
      <select name="type_id" id="component-type">
        <option value="">Select a type…</option>
      </select>
      <button type="button" id="component-new-type" hidden></button>
      <input name="manufacturer" />
      <input name="mpn" />
      <input name="package" />
      <select name="mounting_type">
        <option value="Other" selected>Other</option>
        <option value="SMT">SMT</option>
        <option value="THT">THT</option>
      </select>
      <input name="notes" />
      <p id="component-params-hint"></p>
      <div id="component-params"></div>
      <p id="component-error" hidden></p>
      <button type="button" id="component-open-shop" disabled></button>
      <button type="submit"></button>
    </form></dialog>`;
}

function open(page, ...args) {
  page.window.openComponentDialog(...args);
}

// Give the dialog a real open/close state — the harness stubs showModal with a
// bare vi.fn() that never sets `.open`, so without this a second open() is a fresh
// open, never the reopen path (dialog.open stays false).
function syncOpen(page) {
  const el = page.document.getElementById("component-dialog");
  el.showModal = () => {
    el.open = true;
  };
  el.close = () => {
    el.open = false;
    el.dispatchEvent(new page.window.Event("close"));
  };
  return el;
}

describe("component_dialog.js — Open in shop button", () => {
  it("stays disabled for a plain manual create (no shop behind it)", () => {
    const page = loadPage(dialogFixture(), SCRIPTS);
    open(page, () => {});
    expect(page.document.getElementById("component-open-shop").disabled).toBe(true);
  });

  it("is enabled by an invoice line's prefilled shop URL, and opens it in a new window", () => {
    const page = loadPage(dialogFixture(), SCRIPTS);
    const openWindow = vi.fn();
    page.window.open = openWindow; // jsdom's window.open is unimplemented; stub it

    open(page, () => {}, { shopUrl: "https://www.tme.eu/en/details/SYM1/" });
    const btn = page.document.getElementById("component-open-shop");
    expect(btn.disabled).toBe(false);

    btn.click();
    // Sizing/popup features (not a bare "_blank") make it a standalone window.
    expect(openWindow).toHaveBeenCalledWith(
      "https://www.tme.eu/en/details/SYM1/",
      "_blank",
      "popup,noopener,noreferrer,width=1100,height=850",
    );
  });

  it("is enabled after a shop lookup, targeting the resolved source URL", async () => {
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        url === "/api/shops/lookup"
          ? ok({ mpn: "X", source_url: "https://www.mouser.com/c/?q=X" })
          : ok({}),
    });
    const openWindow = vi.fn();
    page.window.open = openWindow;

    // The scan/import entry point: a code handed in at open time is looked up.
    open(page, () => {}, null, { importCode: "X" });
    await tick();

    const btn = page.document.getElementById("component-open-shop");
    expect(btn.disabled).toBe(false);
    btn.click();
    expect(openWindow).toHaveBeenCalledWith(
      "https://www.mouser.com/c/?q=X",
      "_blank",
      "popup,noopener,noreferrer,width=1100,height=850",
    );
  });

  it("resets to disabled on a fresh manual open after an import", () => {
    const page = loadPage(dialogFixture(), SCRIPTS);
    const btn = page.document.getElementById("component-open-shop");

    open(page, () => {}, { shopUrl: "https://www.tme.eu/en/details/SYM1/" });
    expect(btn.disabled).toBe(false);

    // A later manual open (dialog closed in between) must not keep the old link.
    open(page, () => {});
    expect(btn.disabled).toBe(true);
  });

  it("on a reopen, drops the previous bag's URL and fields (no stale inheritance)", async () => {
    // A bag scanned into the still-open dialog (the queue #76/#77 built drains it
    // there) must fully replace the one under review — the button and the identity
    // fields, not just the code. Bag B is a label-only import (no API key / failed
    // lookup, the ordinary case), so it fills almost nothing and would otherwise
    // inherit bag A's answers.
    let lookupCall = 0;
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (url !== "/api/shops/lookup") return ok({});
        lookupCall += 1;
        return lookupCall === 1
          ? ok({
              mpn: "BAG-A",
              manufacturer: "Acme",
              package: "SOT23",
              description: "widget A",
              source_url: "https://www.tme.eu/en/details/BAG-A/",
            })
          : ok({ mpn: "BAG-B", from_label_only: true }); // nothing but the number
      },
    });
    syncOpen(page);
    const btn = page.document.getElementById("component-open-shop");
    const form = page.document.getElementById("component-form");
    const field = (name) => form.querySelector(`[name="${name}"]`).value;

    // Bag A: a full import — button enabled, identity fields filled.
    open(page, () => {}, null, { importCode: "BAG-A" });
    await tick();
    expect(btn.disabled).toBe(false);
    expect(field("manufacturer")).toBe("Acme");

    // Bag B scanned while bag A's dialog is still open → the reopen path.
    open(page, () => {}, null, { importCode: "BAG-B" });
    // SYNCHRONOUSLY, before the new lookup even resolves, the button must already
    // have dropped bag A's URL — otherwise a click in that window opens bag A's
    // page under bag B's number.
    expect(btn.disabled).toBe(true);
    await tick();

    expect(page.document.getElementById("shop-import-url").value).toBe("BAG-B");
    expect(btn.disabled).toBe(true); // bag B has no shop page — not bag A's
    // Bag A's identity must not survive under bag B's number.
    expect(field("manufacturer")).toBe("");
    expect(field("package")).toBe("");
    expect(field("notes")).toBe("");
  });
});

describe("component_dialog.js — applying the engine's proposal", () => {
  const mounting = (page) =>
    page.document.querySelector('[name="mounting_type"]').value;

  it("applies mounting even when no component type resolves", async () => {
    // A Mouser IC states mounting as "SMD/SMT", which the engine resolves to SMT,
    // but its category may map to no ShelfOS type. Mounting is type-independent, so
    // it must land on the form regardless — it used to be dropped with the rest of
    // the proposal when applyPrefill returned early on an unresolved type.
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: (url) =>
        url === "/api/shops/lookup"
          ? ok({
              mpn: "TMUXHS4412RUAR",
              category: "Multiplexer Switch ICs", // matches no type option
              proposal: {
                type_id: null,
                mounting_type: "SMT",
                package: null,
                parameters: [],
              },
            })
          : ok({}),
    });
    open(page, () => {}, null, { importCode: "TMUXHS4412RUAR" });
    await tick();
    expect(mounting(page)).toBe("SMT");
  });

  it("keeps a staged line's mounting when its type didn't resolve", async () => {
    // An invoice import line the engine couldn't type arrives with typeId "" but a
    // mounting it did infer. That mounting must survive the "no type" early return —
    // the same drop as above, one field over.
    const page = loadPage(dialogFixture(), SCRIPTS);
    open(page, () => {}, { typeId: "", mountingType: "SMT" });
    await tick();
    expect(mounting(page)).toBe("SMT");
  });
});

describe("component_dialog.js — showing what an import left unfilled", () => {
  const tinted = (page) =>
    [...page.document.querySelectorAll("#component-form .is-unfilled")]
      .map(
        (el) =>
          el.name ||
          (el.dataset.definitionId && `param:${el.dataset.definitionId}`) ||
          el.id,
      )
      .sort();

  // A type with one parameter the engine fills and one it doesn't.
  const withParams = (lookup) => (url) => {
    if (url === "/api/shops/lookup") return ok(lookup);
    if (url.endsWith("/parameters")) {
      return ok([
        { id: 10, label: "Resistance", data_type: "number", enum_values: [] },
        { id: 11, label: "Tolerance", data_type: "number", enum_values: [] },
      ]);
    }
    return ok({});
  };

  it("tints only the fields the import had nothing for", async () => {
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: withParams({
        mpn: "MR04X1201FTL",
        manufacturer: "Walsin",
        description: "Resistor: thick film",
        package: null, // the shop said nothing about the case
        proposal: {
          type_id: 1,
          mounting_type: "SMT",
          package: null,
          parameters: [{ parameter_definition_id: 10, value: "1.2k" }],
        },
      }),
    });
    page.document
      .getElementById("component-type")
      .appendChild(new page.window.Option("resistor", "1"));
    open(page, () => {}, null, { importCode: "MR04X1201FTL" });
    await tick();
    await tick();

    // Filled by the import: mpn, manufacturer, notes, type, mounting, Resistance.
    // Left over: the package the shop didn't state, and the Tolerance parameter.
    expect(tinted(page)).toEqual(["package", "param:11"]);
    // The import box itself is a tool, not a field of the component — and it is
    // empty on exactly the successful imports this is meant to annotate.
    const importBox = page.document.getElementById("shop-import-url");
    expect(importBox.classList.contains("is-unfilled")).toBe(false);
  });

  it("clears a field's tint as soon as it is filled or picked", async () => {
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: withParams({ mpn: "X", proposal: null }),
    });
    open(page, () => {}, null, { importCode: "X" });
    await tick();

    const pkg = page.document.querySelector('[name="package"]');
    const mounting = page.document.querySelector('[name="mounting_type"]');
    expect(pkg.classList.contains("is-unfilled")).toBe(true);
    // "Other" is the mounting select's untouched state, so it counts as unfilled.
    expect(mounting.classList.contains("is-unfilled")).toBe(true);

    pkg.value = "0402";
    pkg.dispatchEvent(new page.window.Event("input", { bubbles: true }));
    mounting.value = "SMT";
    mounting.dispatchEvent(new page.window.Event("change", { bubbles: true }));

    expect(pkg.classList.contains("is-unfilled")).toBe(false);
    expect(mounting.classList.contains("is-unfilled")).toBe(false);

    // And emptying it again says so again — nothing is blocked either way.
    pkg.value = "";
    pkg.dispatchEvent(new page.window.Event("input", { bubbles: true }));
    expect(pkg.classList.contains("is-unfilled")).toBe(true);
  });

  it("drops the previous bag's tint the moment the dialog is reopened", async () => {
    // A bag scanned while the previous one is still up reopens the dialog and looks
    // the new code up. Until that answers, the form holds nothing — so bag A's gaps
    // must not still be marked against bag B's number, which would read as
    // information about a part nobody has looked up yet.
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: withParams({ mpn: "BAG-A", proposal: null }),
    });
    const el = syncOpen(page);
    open(page, () => {}, null, { importCode: "BAG-A" });
    await tick();
    await tick();
    expect(tinted(page).length).toBeGreaterThan(0); // bag A's gaps are marked
    expect(el.open).toBe(true); // so the next open takes the reopen path

    open(page, () => {}, null, { importCode: "BAG-B" });
    expect(tinted(page)).toEqual([]);
  });

  it("untints Type when a new type is created for a part that resolved none", async () => {
    // The flow the tint invites: an import that resolved no type is what makes Type
    // a gap, so "+ New type" is the natural next click. The button sets the select
    // in script (no change event) and loads a type whose parameter list may be
    // empty — an early return that used to skip the repaint.
    const page = loadPage(
      dialogFixture() + `<dialog id="type-dialog"></dialog>`,
      SCRIPTS,
      { fetchImpl: (url) => (url.endsWith("/parameters") ? ok([]) : ok({})) },
    );
    let onTypeCreated = null;
    page.window.openTypeDialog = (cb) => {
      onTypeCreated = cb;
    };
    open(page, () => {}, { mpn: "X" }); // a prefill with no type: Type is a gap
    await tick();
    const typeSelect = page.document.getElementById("component-type");
    expect(typeSelect.classList.contains("is-unfilled")).toBe(true);

    page.document.getElementById("component-new-type").click();
    onTypeCreated({ id: 7, name: "screw" });
    await tick();

    expect(typeSelect.value).toBe("7");
    expect(typeSelect.classList.contains("is-unfilled")).toBe(false);
  });

  it("leaves a blank manual create untinted", async () => {
    // Nothing has tried to fill this form, so it has no gaps to point at — every
    // field being red would be noise, not information.
    const page = loadPage(dialogFixture(), SCRIPTS);
    open(page, () => {});
    await tick();
    expect(tinted(page)).toEqual([]);
  });
});

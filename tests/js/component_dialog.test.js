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
      <select name="mounting_type"><option value="Other" selected>Other</option></select>
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

  it("resets to disabled when reopened without a shop source", () => {
    const page = loadPage(dialogFixture(), SCRIPTS);
    const btn = page.document.getElementById("component-open-shop");

    open(page, () => {}, { shopUrl: "https://www.tme.eu/en/details/SYM1/" });
    expect(btn.disabled).toBe(false);

    // A later manual open (dialog closed in between) must not keep the old link.
    open(page, () => {});
    expect(btn.disabled).toBe(true);
  });
});

import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
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
      <div class="field mfr-conflict" id="mfr-conflict" hidden>
        <p class="warn" id="mfr-conflict-summary"></p>
        <ul class="mfr-conflict-list" id="mfr-conflict-list"></ul>
        <p id="mfr-conflict-note" hidden></p>
      </div>
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
    // A stable window name reuses one tab, so repeat opens land in the same place
    // (e.g. the pane the user dragged it to) instead of spawning new windows.
    expect(openWindow).toHaveBeenCalledWith(
      "https://www.tme.eu/en/details/SYM1/",
      "shelfos-shop",
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
      "shelfos-shop",
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

describe("component_dialog.js — you may already have this part", () => {
  // A component's identity is (MPN, manufacturer), and every source spells the
  // maker differently. Rather than guess — a wrong guess FUSES two real parts,
  // where a missed one only duplicates — the dialog shows what the MPN already
  // matches under another name and lets the user say.
  const CANDIDATE = {
    id: 42,
    mpn: "MCP2200",
    manufacturer: "Microchip Technology",
    description: "USB-UART bridge",
    type_name: "ic",
  };

  function conflictFetch(candidates, seen = []) {
    return (url, opts) => {
      seen.push({ url, opts });
      if (url.startsWith("/api/manufacturers/same-mpn")) {
        return ok({ manufacturer: "MICROCHIP", candidates });
      }
      if (url.endsWith("/parameters")) return ok([]);
      return ok({});
    };
  }

  const warning = (page) => page.document.getElementById("mfr-conflict");
  const picks = (page) => [
    ...page.document.querySelectorAll("#mfr-conflict-list button"),
  ];

  it("asks after an import fills the fields", async () => {
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE]),
    });
    open(page, () => {}, { mpn: "MCP2200", manufacturer: "MICROCHIP" });
    await tick();

    expect(warning(page).hidden).toBe(false);
    const name = page.document.querySelector("#mfr-conflict-list .mfr-conflict-name");
    // The maker's name is the whole comparison, so it is the whole row. Asserted as
    // the exact text: the part number is identical by construction and sits in the
    // form two fields up, and printing it again cost the width the button needs.
    expect(name.textContent).toBe("Microchip Technology");
    expect(name.textContent).not.toContain("MCP2200");
    // What is left over is recoverable on hover rather than laid out — it only
    // matters when two real companies share a part number.
    expect(name.title).toBe("ic · USB-UART bridge");
    expect(picks(page)).toHaveLength(1);
  });

  it("keeps the button in the dialog however long the maker's name is", async () => {
    // What actually broke in use: the row outgrew the dialog and "This is it" —
    // the one control the warning exists to offer — needed a horizontal scroll to
    // reach. Under the real app.css the name is the part that yields.
    const css = readFileSync(
      new URL("../../app/web/static/app.css", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(
      `<style>${css}</style>
       <ul class="mfr-conflict-list"><li>
         <span class="mfr-conflict-name">x</span>
         <button class="btn btn-secondary btn-sm">This is it</button>
       </li></ul>`,
    );
    const styleOf = (sel) =>
      dom.window.getComputedStyle(dom.window.document.querySelector(sel));
    expect(styleOf(".mfr-conflict-name").flex).toBe("1 1 0%");
    expect(styleOf(".mfr-conflict-name").textOverflow).toBe("ellipsis");
    expect(styleOf(".mfr-conflict-name").overflow).toBe("hidden");
    // The load-bearing half: a shrinkable button is one a long name can squeeze
    // out of the dialog entirely.
    expect(styleOf(".mfr-conflict-list .btn").flex).toBe("0 0 auto");
  });

  it("says what picking one will teach it, before the click", async () => {
    // The confusion this caused: "This is it" quietly created a global rule that
    // changed how later imports read a manufacturer's name, and nothing said so.
    // It has to be said BEFORE — the components page navigates away the instant a
    // part is chosen, so anything reported afterwards is never read.
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE]),
    });
    open(page, () => {}, { mpn: "MCP2200", manufacturer: "MICROCHIP" });
    await tick();

    const note = page.document.getElementById("mfr-conflict-note");
    expect(note.hidden).toBe(false);
    expect(note.textContent).toContain("MICROCHIP");
    expect(note.textContent).toContain("records");
  });

  it("promises no such thing when nothing would be recorded", async () => {
    // Same spelling as the part already carries: picking it teaches nothing, so
    // claiming otherwise would be a lie about what the button does.
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE]),
    });
    open(page, () => {}, {
      mpn: "MCP2200",
      manufacturer: "microchip technology", // differs only in case
    });
    await tick();

    expect(warning(page).hidden).toBe(false); // still says you own the part
    expect(page.document.getElementById("mfr-conflict-note").hidden).toBe(true);
  });

  it("says nothing when the part number matches nothing", async () => {
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([]),
    });
    open(page, () => {}, { mpn: "MCP2200", manufacturer: "MICROCHIP" });
    await tick();
    expect(warning(page).hidden).toBe(true);
  });

  it("asks nothing at all without a part number", async () => {
    // Every field empty is the ordinary manual create, not a question.
    const seen = [];
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE], seen),
    });
    open(page, () => {});
    await tick();
    expect(seen.filter((r) => r.url.includes("same-mpn"))).toHaveLength(0);
    expect(warning(page).hidden).toBe(true);
  });

  it("asks again when the MPN is typed by hand", async () => {
    const seen = [];
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE], seen),
    });
    open(page, () => {});
    await tick();

    const mpn = page.document.querySelector('[name="mpn"]');
    mpn.value = "MCP2200";
    mpn.dispatchEvent(new page.window.Event("change", { bubbles: true }));
    await tick();

    const asked = seen.filter((r) => r.url.includes("same-mpn"));
    expect(asked).toHaveLength(1);
    // "change", not "input": one request for a finished part number, not one per
    // keystroke of it.
    expect(asked[0].url).toContain("mpn=MCP2200");
    expect(warning(page).hidden).toBe(false);
  });

  it("teaches the alias and answers the dialog with the part picked", async () => {
    const seen = [];
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE], seen),
    });
    const answered = [];
    open(page, (chosen) => answered.push(chosen), {
      mpn: "MCP2200",
      manufacturer: "MICROCHIP",
    });
    await tick();

    // The harness's showModal() is a no-op, so `open` is false either way unless
    // the test puts the dialog in the state the browser would have it in.
    const dialogEl = page.document.getElementById("component-dialog");
    dialogEl.open = true;

    picks(page)[0].click();
    await tick();

    const posted = seen.find((r) => r.url === "/api/manufacturers/aliases");
    expect(posted).toBeTruthy();
    expect(JSON.parse(posted.opts.body)).toEqual({
      alias: "MICROCHIP", // what arrived
      canonical: "Microchip Technology", // what the existing part is filed under
    });
    // Nothing is left to create, so the dialog finishes with the part it found —
    // the same way it finishes with one it made. What the caller then does with it
    // is the caller's business, which is why this does NOT navigate.
    expect(answered).toEqual([CANDIDATE]);
    expect(dialogEl.open).toBe(false);
    expect(page.navigations.length).toBe(0);
  });

  it("still answers with the part when the alias could not be stored", async () => {
    // The alias is a convenience for NEXT time; failing to record it must not
    // strand the user on a dialog for a component that already exists.
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (url === "/api/manufacturers/aliases") return Promise.reject(new Error());
        if (url.startsWith("/api/manufacturers/same-mpn")) {
          return ok({ manufacturer: "MICROCHIP", candidates: [CANDIDATE] });
        }
        return ok({});
      },
    });
    const answered = [];
    open(page, (chosen) => answered.push(chosen), {
      mpn: "MCP2200",
      manufacturer: "MICROCHIP",
    });
    await tick();
    picks(page)[0].click();
    await tick();
    expect(answered).toEqual([CANDIDATE]);
  });

  it("records nothing when there is no spelling to remember", async () => {
    // A Farnell invoice prints no manufacturer column at all, so the question is
    // still worth asking — but there is no alias in the answer.
    const seen = [];
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE], seen),
    });
    const answered = [];
    open(page, (chosen) => answered.push(chosen), { mpn: "MCP2200" });
    await tick();
    picks(page)[0].click();
    await tick();

    expect(seen.some((r) => r.url === "/api/manufacturers/aliases")).toBe(false);
    expect(answered).toEqual([CANDIDATE]); // the choice still stands
  });

  it("asks nothing while editing a staged invoice line", async () => {
    // There "this is it" would have to resolve that LINE to the existing
    // component — a different endpoint, on the invoice panel. Asking a question
    // whose only answer is unavailable is worse than not asking.
    const seen = [];
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE], seen),
    });
    open(page, () => {}, { mpn: "MCP2200", manufacturer: "MICROCHIP" }, {
      stage: { invoiceId: 1, importLineId: 2 },
    });
    await tick();

    expect(seen.filter((r) => r.url.includes("same-mpn"))).toHaveLength(0);
    expect(warning(page).hidden).toBe(true);
  });

  it("ignores an answer about a part number the fields have moved on from", async () => {
    // The MPN field can change faster than the lookups return, and a stale answer
    // would describe a number nobody is looking at any more — as a warning about
    // the one they ARE looking at.
    let call = 0;
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: (url) => {
        if (!url.startsWith("/api/manufacturers/same-mpn")) return ok({});
        call += 1;
        if (call === 1) {
          return new Promise((resolve) =>
            setTimeout(
              () =>
                resolve({
                  ok: true,
                  json: async () => ({
                    manufacturer: "MICROCHIP",
                    candidates: [CANDIDATE],
                  }),
                }),
              30,
            ),
          );
        }
        return ok({ manufacturer: "NXP", candidates: [] });
      },
    });
    open(page, () => {});
    await tick();

    const mpn = page.document.querySelector('[name="mpn"]');
    const change = () =>
      mpn.dispatchEvent(new page.window.Event("change", { bubbles: true }));
    mpn.value = "MCP2200";
    change(); // slow, and about to be superseded
    mpn.value = "NX3P1108";
    change(); // fast, and the one on screen
    await tick();
    await new Promise((resolve) => setTimeout(resolve, 60)); // let the slow one land

    expect(warning(page).hidden).toBe(true);
  });

  it("clears a previous session's candidates when the dialog reopens", async () => {
    // The components page reopens this dialog for the next scanned bag; last bag's
    // candidates describe a different part number entirely.
    const page = loadPage(dialogFixture(), SCRIPTS, {
      fetchImpl: conflictFetch([CANDIDATE]),
    });
    open(page, () => {}, { mpn: "MCP2200", manufacturer: "MICROCHIP" });
    await tick();
    expect(warning(page).hidden).toBe(false);

    page.document.getElementById("component-dialog").open = false;
    open(page, () => {});
    expect(warning(page).hidden).toBe(true); // synchronously, before any lookup
  });
});

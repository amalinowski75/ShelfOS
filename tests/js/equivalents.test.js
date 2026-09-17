import { describe, it, expect } from "vitest";
import {
  loadPage,
  tick,
  CSRF,
  equivalentsWidgetFixture,
} from "./harness.js";

const SCRIPTS = ["shared.js", "equivalents.js"];

const member = (id, mpn, stock, extra = {}) => ({
  component_id: id,
  mpn,
  manufacturer: "AOS",
  package: "SOT-23",
  stock,
  deleted: false,
  ...extra,
});

const alone = { group_id: null, notes: null, total_stock: 40, members: [member(7, "AO3400A", 40)] };

const pair = {
  group_id: 1,
  notes: "tape and bulk of the same die",
  total_stock: 440,
  members: [member(7, "AO3400A", 40), member(9, "AO3400A-TR", 400)],
};

// Answers the panel's own load, the candidate search, and whatever else a test
// wants; every other call falls through to an empty OK.
function feedImpl(group, { candidates = [], extra = () => null } = {}) {
  return (url, opts = {}) => {
    const method = opts.method || "GET";
    if (method === "GET" && /\/equivalents\/candidates/.test(url)) {
      return Promise.resolve({ ok: true, json: async () => candidates });
    }
    if (method === "GET" && /\/equivalents$/.test(url)) {
      return Promise.resolve({ ok: true, json: async () => group });
    }
    return Promise.resolve(extra(url, opts) ?? { ok: true, json: async () => group });
  };
}

describe("equivalents.js", () => {
  it("reads a part on its own as not grouped", async () => {
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(alone),
    });
    await tick();

    // One member is this component itself, which is the "not grouped" state — the
    // table would otherwise show a single row saying the part equals itself.
    expect(document.querySelector(".eq-table-wrap").hidden).toBe(true);
    expect(document.querySelector(".eq-empty").hidden).toBe(false);
    expect(document.querySelector(".eq-note").hidden).toBe(true);
  });

  it("lists the variants with a total, and does not link back to this page", async () => {
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(pair),
    });
    await tick();

    const rows = [...document.querySelectorAll(".eq-rows tr")];
    expect(rows).toHaveLength(2);
    // The component whose page this is renders as plain text; the other is a link.
    expect(rows[0].querySelector("a")).toBeNull();
    expect(rows[1].querySelector("a").getAttribute("href")).toBe("/components/9");
    expect(document.querySelector(".eq-foot").textContent).toContain("440");
    expect(document.querySelector(".eq-note").textContent).toBe(
      "tape and bulk of the same die",
    );
    expect(document.querySelector(".eq-empty").hidden).toBe(true);
  });

  it("marks a member that has been taken out of use", async () => {
    const retired = {
      ...pair,
      members: [member(7, "AO3400A", 40), member(9, "AO3400A-TR", 0, { deleted: true })],
    };
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(retired),
    });
    await tick();

    const rows = [...document.querySelectorAll(".eq-rows tr")];
    expect(rows[1].textContent).toContain("out of use");
    expect(rows[0].textContent).not.toContain("out of use");
  });

  it("says so when the panel cannot be loaded", async () => {
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: () => Promise.reject(new Error("offline")),
    });
    await tick();

    // Not the "nothing is grouped" wording: a failed read is not an answer about
    // the catalogue, and saying it is would hide a real group behind a lie.
    expect(document.querySelector(".eq-empty").textContent).toContain(
      "Could not load",
    );
    expect(document.querySelector(".eq-table-wrap").hidden).toBe(true);
  });

  it("opens the search prefilled with this part's own number", async () => {
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(alone, { candidates: [] }),
    });
    await tick();

    document.querySelector(".eq-add").click();
    await tick();

    // "AO3400A" is the search that finds "AO3400A-TR"; typing it again by hand is
    // the step this saves.
    expect(document.querySelector(".eq-search").value).toBe("AO3400A");
  });

  it("offers a free candidate and refuses one already in another group", async () => {
    const candidates = [
      { component_id: 9, mpn: "AO3400A-TR", manufacturer: "AOS", package: "SOT-23", stock: 400, grouped: false },
      { component_id: 11, mpn: "AO3400A/TRAY", manufacturer: "AOS", package: "SOT-23", stock: 12, grouped: true },
    ];
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(alone, { candidates }),
    });
    await tick();
    document.querySelector(".eq-add").click();
    await tick();

    const rows = [...document.querySelectorAll(".eq-results tr")];
    expect(rows[0].querySelector("button").textContent).toBe("Same part");
    // Listed, but with the reason instead of a button: the server would refuse it,
    // and a failed click looks like a bug.
    expect(rows[1].querySelector("button")).toBeNull();
    expect(rows[1].textContent).toContain("in another group");
  });

  it("posts the chosen part with the note and redraws the group", async () => {
    const candidates = [
      { component_id: 9, mpn: "AO3400A-TR", manufacturer: "AOS", package: "SOT-23", stock: 400, grouped: false },
    ];
    const { document, fetchMock } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(alone, {
        candidates,
        extra: (url, opts) =>
          opts.method === "POST" ? { ok: true, json: async () => pair } : null,
      }),
    });
    await tick();
    document.querySelector(".eq-add").click();
    await tick();
    document.querySelector(".eq-notes").value = "tape and bulk of the same die";

    document.querySelector(".eq-results button").click();
    await tick();

    const post = fetchMock.mock.calls.find(([, opts]) => opts && opts.method === "POST");
    expect(post[0]).toBe("/api/components/7/equivalents");
    expect(post[1].headers["X-CSRF-Token"]).toBe(CSRF);
    const body = JSON.parse(post[1].body);
    expect(body).toEqual({
      component_id: 9,
      notes: "tape and bulk of the same die",
    });
    // The response IS the new group, so the panel redraws from it rather than
    // waiting for a second read.
    expect(document.querySelector(".eq-rows").children).toHaveLength(2);
    // And the dialog stays open, because a part with three variants is added
    // three times.
    expect(document.querySelector(".eq-error").hidden).toBe(true);
  });

  it("shows the server's refusal instead of a silent no-op", async () => {
    const candidates = [
      { component_id: 9, mpn: "SI2302", manufacturer: "AOS", package: "SOT-23", stock: 5, grouped: false },
    ];
    const { document } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(alone, {
        candidates,
        extra: (url, opts) =>
          opts.method === "POST"
            ? {
                ok: false,
                status: 422,
                json: async () => ({ detail: "'SI2302' already belongs to another group" }),
              }
            : null,
      }),
    });
    await tick();
    document.querySelector(".eq-add").click();
    await tick();

    document.querySelector(".eq-results button").click();
    await tick();

    const error = document.querySelector(".eq-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("another group");
  });

  it("removes a member through the component whose page is open", async () => {
    const { document, fetchMock } = loadPage(equivalentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(pair, {
        extra: (url, opts) =>
          opts.method === "DELETE" ? { ok: true, json: async () => ({}) } : null,
      }),
    });
    await tick();

    const remove = [...document.querySelectorAll(".eq-rows button")].at(-1);
    remove.click();
    await tick();

    const del = fetchMock.mock.calls.find(([, opts]) => opts && opts.method === "DELETE");
    // Addressed through this page's component, so the server can check the pair
    // belongs together before dissolving anything.
    expect(del[0]).toBe("/api/components/7/equivalents/9");
    expect(del[1].headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("gives a read-only account no way to change the group", async () => {
    const { document } = loadPage(
      equivalentsWidgetFixture({ withDialog: false }),
      SCRIPTS,
      { fetchImpl: feedImpl(pair), role: "read-only" },
    );
    await tick();

    expect(document.querySelectorAll(".eq-rows tr")).toHaveLength(2);
    expect(document.querySelectorAll(".eq-rows button")).toHaveLength(0);
  });
});

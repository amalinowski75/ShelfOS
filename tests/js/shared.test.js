import { describe, it, expect } from "vitest";
import { loadPage, CSRF } from "./harness.js";

describe("shared.js", () => {
  it("reads the CSRF token from the meta tag", () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    expect(window.eval("csrfToken")).toBe(CSRF);
  });

  it("canWrite reflects the user-role meta", () => {
    const writable = (role) => {
      const { window } = loadPage("<div></div>", ["shared.js"], { role });
      return window.eval("canWrite");
    };
    expect(writable("admin")).toBe(true);
    expect(writable("user")).toBe(true);
    expect(writable("read-only")).toBe(false);
    // No role (logged-out / no meta) is treated as non-writer.
    expect(writable("")).toBe(false);
  });

  it("fetchAttachmentList caches per URL and refetches only when fresh", async () => {
    const { window, fetchMock } = loadPage("<div></div>", ["shared.js"], {
      fetchImpl: () => Promise.resolve({ ok: true, json: async () => [{ id: 1 }] }),
    });
    const get = window.eval("(url, opts) => fetchAttachmentList(url, opts)");

    await get("/api/attachments?x");
    await get("/api/attachments?x");
    expect(fetchMock).toHaveBeenCalledTimes(1); // second call served from cache

    await get("/api/attachments?x", { fresh: true });
    expect(fetchMock).toHaveBeenCalledTimes(2); // fresh bypasses the cache
  });

  it("fetchAttachmentList does not cache a failed fetch (retries next call)", async () => {
    let calls = 0;
    const fetchImpl = () => {
      calls += 1;
      return calls === 1
        ? Promise.reject(new Error("down"))
        : Promise.resolve({ ok: true, json: async () => [{ id: 1 }] });
    };
    const { window } = loadPage("<div></div>", ["shared.js"], { fetchImpl });
    const get = window.eval("(url) => fetchAttachmentList(url)");

    await expect(get("/api/attachments?x")).rejects.toThrow();
    // The failed fetch wasn't cached, so this retries and succeeds.
    expect(await get("/api/attachments?x")).toEqual([{ id: 1 }]);
  });

  it("esc() escapes every HTML metacharacter", () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    expect(window.esc(`<a href="x">&'`)).toBe(
      "&lt;a href=&quot;x&quot;&gt;&amp;&#39;",
    );
  });

  it("esc() renders null/undefined as an empty string", () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    expect(window.esc(null)).toBe("");
    expect(window.esc(undefined)).toBe("");
  });

  it("errorMessage() surfaces a string detail", async () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    const msg = await window.errorMessage({
      json: async () => ({ detail: "boom" }),
    });
    expect(msg).toBe("boom");
  });

  it("errorMessage() joins a list-shaped 422 detail", async () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    const msg = await window.errorMessage({
      json: async () => ({ detail: [{ msg: "a" }, { msg: "b" }] }),
    });
    expect(msg).toBe("a; b");
  });

  it("errorMessage() falls back when the body is not JSON", async () => {
    const { window } = loadPage("<div></div>", ["shared.js"]);
    const msg = await window.errorMessage(
      {
        json: async () => {
          throw new Error("not json");
        },
      },
      "custom fallback",
    );
    expect(msg).toBe("custom fallback");
  });

  it("wires [data-close] buttons to close their dialog", () => {
    const { document } = loadPage(
      `<dialog id="d"><button data-close></button></dialog>`,
      ["shared.js"],
    );
    const dialog = document.getElementById("d");
    document.querySelector("[data-close]").click();
    expect(dialog.close).toHaveBeenCalled();
  });
});

describe("shared.js — match-rule alias lists", () => {
  // One rule per alias is still what the engine holds; these helpers only turn a
  // target's rules into one comma-separated field and back.
  const load = () => loadPage("<div></div>", ["shared.js"]).window;

  it("splits a list, dropping blanks and keeping a repeat once", () => {
    const window = load();
    expect(window.splitAliases(" biały, czarny ,, biały , BIAŁY ")).toEqual([
      "biały",
      "czarny",
    ]);
    // The first spelling is the one kept — it is what the user actually typed.
    expect(window.splitAliases("")).toEqual([]);
    expect(window.splitAliases(null)).toEqual([]);
  });

  it("groups rules by domain + target + scope, lowest order winning", () => {
    const window = load();
    const grouped = window.groupRulesByTarget([
      { id: 1, domain: "enum_value", alias: "biały", canonical: "Kolor", parameter_definition_id: 9, sort_order: 4 },
      { id: 2, domain: "enum_value", alias: "czarny", canonical: "Kolor", parameter_definition_id: 9, sort_order: 1 },
      // Same target text, different parameter — a different rule entirely.
      { id: 3, domain: "enum_value", alias: "biały", canonical: "Kolor", parameter_definition_id: 8, sort_order: 0 },
      // Same target text, different domain — likewise.
      { id: 4, domain: "param_name", alias: "kolor", canonical: "Kolor", parameter_definition_id: null, sort_order: 0 },
    ]);
    expect(grouped.map((g) => [g.alias, g.parameter_definition_id, g.domain])).toEqual([
      ["biały, czarny", 9, "enum_value"],
      ["biały", 8, "enum_value"],
      ["kolor", null, "param_name"],
    ]);
    // The engine takes the first matching rule in sort order, so the group's
    // precedence is its lowest — not the first row's.
    expect(grouped[0].sort_order).toBe(1);
    expect(grouped[0].rules.map((r) => r.id)).toEqual([1, 2]);
  });

  it("turns an edited list into renames, removals and additions", () => {
    const window = load();
    const group = {
      domain: "enum_value",
      canonical: "Kolor",
      parameter_definition_id: 9,
      sort_order: 3,
      rules: [
        { id: 1, alias: "biały" },
        { id: 2, alias: "czarny" },
      ],
    };
    // One word swapped for another: a rename in place, so the audit entry reads as
    // an edit and a case-only fix can't collide with the rule it replaces.
    expect(window.aliasListWrites(group, "biały, szary")).toEqual([
      { method: "PATCH", id: 2, body: { alias: "szary" } },
    ]);
    // Fewer than before: the leftover rule goes.
    expect(window.aliasListWrites(group, "biały")).toEqual([
      { method: "DELETE", id: 2 },
    ]);
    // More than before: a new rule onto the same target, at the group's order.
    expect(window.aliasListWrites(group, "biały, czarny, różowy")).toEqual([
      {
        method: "POST",
        body: {
          domain: "enum_value",
          alias: "różowy",
          canonical: "Kolor",
          parameter_definition_id: 9,
          sort_order: 3,
        },
      },
    ]);
    // Nothing changed is nothing written, whatever the spacing.
    expect(window.aliasListWrites(group, " czarny ,biały ")).toEqual([]);
    // An empty list is refused: clearing the field is not how a target is removed.
    expect(window.aliasListWrites(group, " , ")).toBeNull();
  });
});

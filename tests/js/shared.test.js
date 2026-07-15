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

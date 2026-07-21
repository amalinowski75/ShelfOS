import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, linksWidgetFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "links.js"];

// A fetch that answers the initial list load, plus whatever the test needs.
function feedImpl(rows, extra = () => null) {
  return (url, opts = {}) => {
    const method = opts.method || "GET";
    if (method === "GET" && url.startsWith("/api/links?")) {
      return Promise.resolve({ ok: true, json: async () => rows });
    }
    return Promise.resolve(extra(url, opts) ?? { ok: true, json: async () => ({}) });
  };
}

function submitForm(document, window) {
  document.querySelector(".link-form").dispatchEvent(
    new window.Event("submit", { cancelable: true, bubbles: true }),
  );
}

describe("links.js", () => {
  it("renders an external link (new tab, noopener) with a kind badge", async () => {
    const rows = [
      {
        id: 3,
        url: "https://www.tme.eu/pl/details/x/y/",
        label: "TME page",
        kind: "shop",
        notes: "imported from here",
      },
    ];
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();

    const item = document.querySelector(".link-item");
    const anchor = item.querySelector("a");
    expect(anchor.getAttribute("href")).toBe("https://www.tme.eu/pl/details/x/y/");
    expect(anchor.getAttribute("target")).toBe("_blank");
    expect(anchor.getAttribute("rel")).toBe("noopener noreferrer");
    expect(anchor.textContent).toBe("TME page");
    expect(item.textContent).toContain("shop");
    expect(item.textContent).toContain("imported from here");
    expect(item.querySelector("button").textContent).toBe("Delete");
  });

  it("falls back to the URL host when there is no label", async () => {
    const rows = [{ id: 1, url: "https://datasheet.example.com/a.pdf", kind: "datasheet" }];
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();
    expect(document.querySelector(".link-item a").textContent).toBe(
      "datasheet.example.com",
    );
  });

  // The server rejects these on create; the renderer is the second layer, so it must
  // hold for a row that (however it got there) bypassed server validation — including
  // the cases where the renderer's rule differs from the server's.
  it.each([
    ["a javascript: scheme", "javascript:alert(1)"],
    ["a data: scheme", "data:text/html,<b>x</b>"],
    ["a leading control char", "\x01https://x.io"],
    ["a scheme-relative URL", "//evil.example.com"],
  ])("renders %s as inert text, never a live link", async (_name, url) => {
    const rows = [{ id: 1, url, label: "click me", kind: "other" }];
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();
    const item = document.querySelector(".link-item");
    expect(item.querySelector("a")).toBeNull();
    expect(item.querySelector("span.cell-mono").textContent).toBe("click me");
  });

  it("still renders a mixed-case http(s) scheme as a live link", async () => {
    // The renderer's check is case-insensitive, matching the browser (and the
    // server, which lowercases the scheme in urlsplit) — so this must stay clickable.
    const rows = [{ id: 1, url: "HTTPS://x.io/a", kind: "other" }];
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();
    expect(document.querySelector(".link-item a").getAttribute("href")).toBe(
      "HTTPS://x.io/a",
    );
  });

  it("escapes url, label and notes", async () => {
    const rows = [
      {
        id: 1,
        url: "https://x.io/<b>",
        label: "<i>lbl",
        kind: "other",
        notes: "<u>n",
      },
    ];
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();
    const anchor = document.querySelector(".link-item a");
    expect(anchor.getAttribute("href")).toBe("https://x.io/<b>"); // attr, not markup
    expect(anchor.innerHTML).toBe("&lt;i&gt;lbl");
    expect(document.querySelector(".link-notes").innerHTML).toBe("&lt;u&gt;n");
  });

  it("does not let a quote in the URL break out of the href attribute", async () => {
    // The real attribute-breakout char is the double quote, not <>. This pins that
    // the href value is escaped, so a crafted URL can't inject an event handler.
    const rows = [
      { id: 1, url: 'https://x.io/a"onmouseover="alert(1)', kind: "other" },
    ];
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();
    const anchor = document.querySelector(".link-item a");
    // The whole crafted string is the href; no stray onmouseover attribute exists.
    expect(anchor.getAttribute("href")).toBe('https://x.io/a"onmouseover="alert(1)');
    expect(anchor.hasAttribute("onmouseover")).toBe(false);
  });

  it("shows the empty state when there are no links", async () => {
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    expect(document.querySelector(".link-empty").hidden).toBe(false);
    expect(document.querySelectorAll(".link-item")).toHaveLength(0);
  });

  it("opens the add dialog from the + Add button", async () => {
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    const dialog = document.querySelector(".link-dialog");
    dialog.showModal = vi.fn();
    document.querySelector(".link-add").click();
    expect(dialog.showModal).toHaveBeenCalled();
  });

  it("closes the add dialog after a successful add", async () => {
    const { window, document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    const dialog = document.querySelector(".link-dialog");
    dialog.close = vi.fn();
    document.querySelector(".link-form").elements.url.value = "https://x.io";
    submitForm(document, window);
    await tick();
    expect(dialog.close).toHaveBeenCalled();
  });

  it("posts JSON with the CSRF token when the form is submitted", async () => {
    const { window, document, fetchMock } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    const form = document.querySelector(".link-form");
    form.elements.url.value = "https://example.com/x";
    form.elements.kind.value = "shop";
    form.elements.label.value = "Example";
    submitForm(document, window);
    await tick();

    const call = fetchMock.mock.calls.find(
      (c) => c[0] === "/api/links" && c[1]?.method === "POST",
    );
    expect(call).toBeTruthy();
    expect(call[1].headers["Content-Type"]).toBe("application/json");
    expect(call[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(call[1].body)).toMatchObject({
      entity_type: "component",
      entity_id: 7,
      url: "https://example.com/x",
      kind: "shop",
      label: "Example",
    });
  });

  it("refuses to submit an empty URL", async () => {
    const { window, document, fetchMock } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    submitForm(document, window);
    await tick();
    expect(document.querySelector(".link-error").hidden).toBe(false);
    expect(fetchMock.mock.calls.some((c) => c[1]?.method === "POST")).toBe(false);
  });

  it("surfaces a server rejection", async () => {
    const { window, document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([], (url, opts) =>
        opts.method === "POST"
          ? { ok: false, json: async () => ({ detail: "only http and https links are allowed" }) }
          : null,
      ),
    });
    await tick();
    const dialog = document.querySelector(".link-dialog");
    dialog.close = vi.fn();
    document.querySelector(".link-form").elements.url.value = "https://x.io";
    submitForm(document, window);
    await tick();
    const error = document.querySelector(".link-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("only http and https links are allowed");
    expect(dialog.close).not.toHaveBeenCalled(); // stays open to fix and retry
  });

  it("closes the dialog via the shared [data-close] control", async () => {
    const { document } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    const dialog = document.querySelector(".link-dialog");
    dialog.close = vi.fn();
    document.querySelector(".link-dialog [data-close]").click();
    expect(dialog.close).toHaveBeenCalled();
  });

  it("deletes after confirmation, with the CSRF token", async () => {
    const rows = [{ id: 9, url: "https://x.io", label: "X", kind: "other" }];
    const { window, document, fetchMock } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    window.confirm = vi.fn(() => true);
    await tick();

    document.querySelector(".link-item button").click();
    await tick();

    const del = fetchMock.mock.calls.find((c) => c[1]?.method === "DELETE");
    expect(del[0]).toBe("/api/links/9");
    expect(del[1].headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("does not delete when the confirm is declined", async () => {
    const rows = [{ id: 9, url: "https://x.io", label: "X", kind: "other" }];
    const { window, document, fetchMock } = loadPage(linksWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    window.confirm = vi.fn(() => false);
    await tick();
    document.querySelector(".link-item button").click();
    await tick();
    expect(fetchMock.mock.calls.some((c) => c[1]?.method === "DELETE")).toBe(false);
  });

  it("gives a read-only account no delete buttons and no add form", async () => {
    const rows = [{ id: 1, url: "https://x.io", label: "X", kind: "other" }];
    const { document } = loadPage(linksWidgetFixture({ withForm: false }), SCRIPTS, {
      fetchImpl: feedImpl(rows),
      role: "read-only",
    });
    await tick();
    expect(document.querySelectorAll(".link-item")).toHaveLength(1);
    expect(document.querySelector(".link-item button")).toBeNull();
    expect(document.querySelector(".link-form")).toBeNull();
    // The add affordance is writer-only too — no "+ Add" button, no dialog.
    expect(document.querySelector(".link-add")).toBeNull();
    expect(document.querySelector(".link-dialog")).toBeNull();
  });
});

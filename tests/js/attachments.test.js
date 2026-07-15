import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, attachmentsWidgetFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "attachments.js"];

// A fetch that answers the initial list load, plus whatever the test needs.
function feedImpl(rows, extra = () => null) {
  return (url, opts = {}) => {
    const method = opts.method || "GET";
    if (method === "GET" && url.startsWith("/api/attachments?")) {
      return Promise.resolve({ ok: true, json: async () => rows });
    }
    return Promise.resolve(extra(url, opts) ?? { ok: true, json: async () => ({}) });
  };
}

describe("attachments.js", () => {
  it("lists attachments with a download link, kind badge and delete button", async () => {
    const rows = [
      { id: 3, filename: "ds.pdf", kind: "datasheet", notes: "rev B" },
      { id: 4, filename: "photo.jpg", kind: "photo", notes: null },
    ];
    const { document } = loadPage(attachmentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();

    const items = document.querySelectorAll(".attachment-item");
    expect(items).toHaveLength(2);
    const link = items[0].querySelector("a");
    expect(link.getAttribute("href")).toBe("/api/attachments/3/download");
    expect(link.textContent).toBe("ds.pdf");
    expect(items[0].textContent).toContain("datasheet");
    expect(items[0].textContent).toContain("rev B");
    // Writer sees a Delete button per row.
    expect(items[0].querySelector("button").textContent).toBe("Delete");
  });

  it("shows the empty state when there are no attachments", async () => {
    const { document } = loadPage(attachmentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl([]),
    });
    await tick();
    expect(document.querySelector(".attachment-empty").hidden).toBe(false);
    expect(document.querySelectorAll(".attachment-item")).toHaveLength(0);
  });

  it("escapes attachment fields", async () => {
    const rows = [{ id: 1, filename: "<b>x", kind: "other", notes: null }];
    const { document } = loadPage(attachmentsWidgetFixture(), SCRIPTS, {
      fetchImpl: feedImpl(rows),
    });
    await tick();
    expect(document.querySelector(".attachment-item a").innerHTML).toBe("&lt;b&gt;x");
  });

  it("uploads via multipart FormData with the CSRF token and no Content-Type", async () => {
    const { window, document, fetchMock } = loadPage(
      attachmentsWidgetFixture(),
      SCRIPTS,
      { fetchImpl: feedImpl([]) },
    );
    await tick();

    document.querySelector(".attachment-form").dispatchEvent(
      new window.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    const post = fetchMock.mock.calls.find((c) => (c[1]?.method || "GET") === "POST");
    expect(post[0]).toBe("/api/attachments");
    expect(post[1].headers["X-CSRF-Token"]).toBe(CSRF);
    expect(post[1].headers["Content-Type"]).toBeUndefined();
    const body = post[1].body;
    expect(body).toBeInstanceOf(window.FormData);
    expect(body.get("entity_type")).toBe("component");
    expect(body.get("entity_id")).toBe("7");
  });

  it("deletes after confirmation, with the CSRF token", async () => {
    const rows = [{ id: 9, filename: "ds.pdf", kind: "datasheet", notes: null }];
    const { window, document, fetchMock } = loadPage(
      attachmentsWidgetFixture(),
      SCRIPTS,
      { fetchImpl: feedImpl(rows) },
    );
    window.confirm = vi.fn(() => true);
    await tick();

    document.querySelector(".attachment-item button").click();
    await tick();

    const del = fetchMock.mock.calls.find((c) => c[1]?.method === "DELETE");
    expect(del[0]).toBe("/api/attachments/9");
    expect(del[1].headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("does not delete when the confirm is declined", async () => {
    const rows = [{ id: 9, filename: "ds.pdf", kind: "datasheet", notes: null }];
    const { window, document, fetchMock } = loadPage(
      attachmentsWidgetFixture(),
      SCRIPTS,
      { fetchImpl: feedImpl(rows) },
    );
    window.confirm = vi.fn(() => false);
    await tick();

    document.querySelector(".attachment-item button").click();
    await tick();
    expect(fetchMock.mock.calls.some((c) => c[1]?.method === "DELETE")).toBe(false);
  });

  it("surfaces a server rejection from the upload", async () => {
    const fetchImpl = (url, opts = {}) =>
      (opts.method || "GET") === "POST"
        ? Promise.resolve({ ok: false, json: async () => ({ detail: "too big" }) })
        : Promise.resolve({ ok: true, json: async () => [] });
    const { window, document } = loadPage(attachmentsWidgetFixture(), SCRIPTS, {
      fetchImpl,
    });
    await tick();

    document.querySelector(".attachment-form").dispatchEvent(
      new window.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    const error = document.querySelector(".attachment-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("too big");
  });

  it("shows a reach-the-server message when the upload throws", async () => {
    const fetchImpl = (url, opts = {}) =>
      (opts.method || "GET") === "POST"
        ? Promise.reject(new Error("network"))
        : Promise.resolve({ ok: true, json: async () => [] });
    const { window, document } = loadPage(attachmentsWidgetFixture(), SCRIPTS, {
      fetchImpl,
    });
    await tick();

    document.querySelector(".attachment-form").dispatchEvent(
      new window.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    const error = document.querySelector(".attachment-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Could not reach the server.");
  });

  it("shows an error instead of a blank panel when the list fails to load", async () => {
    const { document } = loadPage(attachmentsWidgetFixture(), SCRIPTS, {
      fetchImpl: () => Promise.reject(new Error("network")),
    });
    await tick();

    const empty = document.querySelector(".attachment-empty");
    expect(empty.hidden).toBe(false);
    expect(empty.textContent).toContain("Could not load");
  });

  it("gives a read-only account no delete buttons and no upload form", async () => {
    const rows = [{ id: 1, filename: "ds.pdf", kind: "datasheet", notes: null }];
    const { document } = loadPage(
      attachmentsWidgetFixture({ withForm: false }),
      SCRIPTS,
      { fetchImpl: feedImpl(rows), role: "read-only" },
    );
    await tick();

    expect(document.querySelectorAll(".attachment-item")).toHaveLength(1);
    expect(document.querySelector(".attachment-item button")).toBeNull();
    expect(document.querySelector(".attachment-form")).toBeNull();
  });
});

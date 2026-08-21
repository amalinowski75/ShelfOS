import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF, bomUploadFixture, bomListFixture } from "./harness.js";

const SCRIPTS = ["shared.js", "boms.js"];

function submit(document) {
  document
    .getElementById("bom-upload-form")
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("boms.js — import", () => {
  it("opens the import dialog from the button", () => {
    const { window, document } = loadPage(bomUploadFixture(), SCRIPTS);
    document.getElementById("bom-upload-btn").click();
    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
  });

  it("uploads via multipart FormData with the CSRF token and no Content-Type", async () => {
    const { window, document, fetchMock } = loadPage(bomUploadFixture(), SCRIPTS);
    document.getElementById("bom-upload-btn").click();
    submit(document);
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/boms");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(opts.headers["Content-Type"]).toBeUndefined();
    expect(opts.body).toBeInstanceOf(window.FormData);
  });

  it("does not show an error on a successful import", async () => {
    // Default fetch is ok:{id:42}; success navigates to the report (jsdom swallows
    // the navigation), so assert the success branch ran — no error surfaced.
    const { document, fetchMock } = loadPage(bomUploadFixture(), SCRIPTS);
    document.getElementById("bom-upload-btn").click();
    submit(document);
    await tick();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/boms");
    expect(document.getElementById("bom-upload-error").hidden).toBe(true);
  });

  it("surfaces a server rejection (e.g. a malformed BOM)", async () => {
    const fetchImpl = () =>
      Promise.resolve({ ok: false, json: async () => ({ detail: "malformed BOM" }) });
    const { document } = loadPage(bomUploadFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("bom-upload-btn").click();
    submit(document);
    await tick();

    const error = document.getElementById("bom-upload-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("malformed BOM");
  });

  it("shows a reach-the-server message when the request throws", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { document } = loadPage(bomUploadFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("bom-upload-btn").click();
    submit(document);
    await tick();

    const error = document.getElementById("bom-upload-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Could not reach the server.");
  });
});

describe("boms.js — delete", () => {
  const deleteBtn = (document) => document.querySelector("[data-bom-delete]");

  it("confirms, then DELETEs the BOM with the CSRF token", async () => {
    const { window, document, fetchMock } = loadPage(bomListFixture(), SCRIPTS);
    deleteBtn(document).click();
    await tick();

    expect(window.confirm).toHaveBeenCalled();
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/boms/7");
    expect(opts.method).toBe("DELETE");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("names the BOM in the confirmation — a delete takes the CSV with it", () => {
    const { window, document } = loadPage(bomListFixture(), SCRIPTS);
    deleteBtn(document).click();
    expect(window.confirm.mock.calls[0][0]).toContain("Hiduart");
  });

  it("sends nothing when the confirmation is declined", async () => {
    const { window, document, fetchMock } = loadPage(bomListFixture(), SCRIPTS);
    window.confirm = () => false;
    deleteBtn(document).click();
    await tick();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces a refusal instead of reloading", async () => {
    const fetchImpl = () =>
      Promise.resolve({ ok: false, json: async () => ({ detail: "nope" }) });
    const { window, document } = loadPage(bomListFixture(), SCRIPTS, { fetchImpl });
    deleteBtn(document).click();
    await tick();
    expect(window.alert).toHaveBeenCalledWith("nope");
  });
});

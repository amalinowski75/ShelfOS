import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

const SCRIPTS = ["shared.js", "location_dialog.js"];

function locationDialogFixture(options = [{ id: 1, path: "Lab" }]) {
  const opts = options
    .map((o) => `<option value="${o.id}">${o.path}</option>`)
    .join("");
  return `
    <button id="new-location-btn"></button>
    <dialog id="location-dialog"><form id="location-form">
      <select name="type">
        <option value="room">room</option>
        <option value="rack">rack</option>
      </select>
      <input name="name" />
      <select name="parent_id"><option value="">None</option>${opts}</select>
      <p id="location-error" hidden></p>
      <button type="submit"></button>
    </form></dialog>`;
}

function submit(document) {
  document
    .getElementById("location-form")
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("location_dialog.js", () => {
  it("posts type/name/parent, with parent_id null when unset", async () => {
    const { window, document, fetchMock } = loadPage(
      locationDialogFixture(),
      SCRIPTS,
      {
        fetchImpl: () =>
          Promise.resolve({
            ok: true,
            json: async () => ({ id: 9, name: "Rack A", type: "rack" }),
          }),
      },
    );
    const created = [];
    window.openLocationDialog((c) => created.push(c));
    document.querySelector('[name="type"]').value = "rack";
    document.querySelector('[name="name"]').value = "Rack A";
    submit(document);
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/locations");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({
      type: "rack",
      name: "Rack A",
      parent_id: null,
    });
    expect(created).toEqual([{ id: 9, name: "Rack A", type: "rack" }]);
  });

  it("sends parent_id as a number when a parent is chosen", async () => {
    const { window, document, fetchMock } = loadPage(locationDialogFixture(), SCRIPTS);
    window.openLocationDialog();
    document.querySelector('[name="name"]').value = "Shelf 1";
    document.querySelector('[name="parent_id"]').value = "1";
    submit(document);
    await tick();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).parent_id).toBe(1);
  });

  it("surfaces the server error", async () => {
    const { window, document } = loadPage(locationDialogFixture(), SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({ ok: false, json: async () => ({ detail: "duplicate" }) }),
    });
    window.openLocationDialog();
    document.querySelector('[name="name"]').value = "Lab";
    submit(document);
    await tick();
    const error = document.getElementById("location-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("duplicate");
  });

  it("surfaces a network failure instead of an unhandled rejection", async () => {
    const { window, document } = loadPage(locationDialogFixture(), SCRIPTS, {
      fetchImpl: () => Promise.reject(new Error("down")),
    });
    window.openLocationDialog();
    document.querySelector('[name="name"]').value = "Lab";
    submit(document);
    await tick();
    expect(document.getElementById("location-error").hidden).toBe(false);
  });

  it("ignores a re-entrant submit while a create is in flight", async () => {
    let resolveFetch;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    const { window, document, fetchMock } = loadPage(locationDialogFixture(), SCRIPTS, {
      fetchImpl: () => pending,
    });
    window.openLocationDialog();
    document.querySelector('[name="name"]').value = "Rack A";
    submit(document); // first submit — fetch is now in flight
    submit(document); // a fast double-click must be ignored, not POST again
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveFetch({
      ok: true,
      json: async () => ({ id: 9, name: "Rack A", type: "rack" }),
    });
    await tick();
  });

  it("the standalone New Location button opens the dialog", () => {
    const { document } = loadPage(locationDialogFixture(), SCRIPTS);
    const showModal = vi.fn();
    document.getElementById("location-dialog").showModal = showModal;
    document.getElementById("new-location-btn").click();
    expect(showModal).toHaveBeenCalled();
  });
});

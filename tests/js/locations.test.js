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

  it("adds the created location to the parent select for immediate nesting", async () => {
    const { window, document } = loadPage(
      locationDialogFixture([{ id: 1, path: "Lab" }]),
      SCRIPTS,
      {
        fetchImpl: () =>
          Promise.resolve({
            ok: true,
            json: async () => ({ id: 9, name: "Rack A", type: "rack", parent_id: 1 }),
          }),
      },
    );
    window.openLocationDialog();
    document.querySelector('[name="name"]').value = "Rack A";
    document.querySelector('[name="parent_id"]').value = "1"; // under Lab
    submit(document);
    await tick();

    // The new location is now selectable as a parent, pathed under Lab.
    const parentSelect = document.querySelector('[name="parent_id"]');
    const added = [...parentSelect.options].find((o) => o.value === "9");
    expect(added).toBeTruthy();
    expect(added.text).toBe("Lab / Rack A");
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

// The dialog markup with the header/submit elements edit mode relabels, and a
// small hierarchy in the parent select: Lab(1) > Rack A(5) > Bin(9), Bench(2).
function editableDialogFixture() {
  return `
    <dialog id="location-dialog">
      <strong id="location-dialog-title">New location</strong>
      <form id="location-form">
        <select name="type">
          <option value="room">room</option>
          <option value="rack">rack</option>
          <option value="shelf">shelf</option>
        </select>
        <input name="name" />
        <select name="parent_id">
          <option value="">None</option>
          <option value="1">Lab</option>
          <option value="5">Lab / Rack A</option>
          <option value="9">Lab / Rack A / Bin</option>
          <option value="2">Bench</option>
        </select>
        <p id="location-error" hidden></p>
        <button type="submit" id="location-submit">Create location</button>
      </form>
    </dialog>`;
}

describe("location_dialog.js — edit mode", () => {
  const rackA = {
    id: 5,
    name: "Rack A",
    type: "rack",
    parentId: 1,
    disabledIds: [5, 9],
  };

  it("prefills the form and PATCHes the edited location", async () => {
    const done = [];
    const { window, document, fetchMock } = loadPage(
      editableDialogFixture(),
      SCRIPTS,
      {
        fetchImpl: () =>
          Promise.resolve({
            ok: true,
            json: async () => ({ id: 5, name: "Rack B", type: "rack" }),
          }),
      },
    );
    window.openLocationDialog((saved) => done.push(saved), rackA);

    expect(document.getElementById("location-dialog-title").textContent).toBe(
      "Edit location",
    );
    expect(document.getElementById("location-submit").textContent).toBe(
      "Save changes",
    );
    expect(document.querySelector('[name="name"]').value).toBe("Rack A");
    expect(document.querySelector('[name="type"]').value).toBe("rack");
    expect(document.querySelector('[name="parent_id"]').value).toBe("1");

    document.querySelector('[name="name"]').value = "Rack B";
    submit(document);
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/locations/5");
    expect(opts.method).toBe("PATCH");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({
      type: "rack",
      name: "Rack B",
      parent_id: 1,
    });
    expect(done).toEqual([{ id: 5, name: "Rack B", type: "rack" }]);
  });

  it("bars itself and its descendants as the new parent", () => {
    const { window, document } = loadPage(editableDialogFixture(), SCRIPTS);
    window.openLocationDialog(null, rackA);
    const option = (value) =>
      document.querySelector(`[name="parent_id"] option[value="${value}"]`);
    expect(option("5").disabled).toBe(true); // itself
    expect(option("9").disabled).toBe(true); // its descendant
    expect(option("1").disabled).toBe(false); // its parent stays offered
    expect(option("2").disabled).toBe(false);
  });

  it("create mode preselects the parent passed as a default", () => {
    const { window, document } = loadPage(editableDialogFixture(), SCRIPTS);
    window.openLocationDialog(null, null, { parentId: 5 });
    expect(document.getElementById("location-dialog-title").textContent).toBe(
      "New location",
    );
    expect(document.querySelector('[name="parent_id"]').value).toBe("5");
  });

  it("a later create-open resets the labels and re-enables every parent", () => {
    const { window, document } = loadPage(editableDialogFixture(), SCRIPTS);
    window.openLocationDialog(null, rackA);
    window.openLocationDialog();
    expect(document.getElementById("location-dialog-title").textContent).toBe(
      "New location",
    );
    expect(document.getElementById("location-submit").textContent).toBe(
      "Create location",
    );
    for (const opt of document.querySelectorAll('[name="parent_id"] option')) {
      expect(opt.disabled).toBe(false);
    }
  });
});

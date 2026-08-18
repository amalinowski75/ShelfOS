import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF, fetchBody } from "./harness.js";

const SCRIPTS = ["shared.js", "label_print.js"];

// The dialog's markup, mirroring _label_print_dialog.html.
const FIXTURE = `
  <dialog id="label-print-dialog">
    <form id="label-print-form">
      <p id="label-print-what"></p>
      <select class="control" id="label-print-tape"></select>
      <p id="label-print-tape-hint"></p>
      <img id="label-print-preview" alt="" />
      <div id="label-print-progress" hidden>
        <p id="label-print-progress-text"></p>
        <button type="button" id="label-print-stop">Stop printing</button>
      </div>
      <p id="label-print-error" hidden></p>
      <div id="label-print-mismatch" hidden>
        <p id="label-print-mismatch-text"></p>
        <button type="button" id="label-print-use-loaded">Print on the loaded tape</button>
        <button type="button" id="label-print-recheck">I changed the roll</button>
      </div>
      <button type="submit" id="label-print-submit">Print</button>
    </form>
  </dialog>`;

const TAPES = {
  tapes: [
    { id: "62", name: "62 mm continuous", width_mm: 62, length_mm: null, two_color: false },
    { id: "62red", name: "62 mm continuous, black/red", width_mm: 62, length_mm: null, two_color: true },
    { id: "62x29", name: "62 × 29 mm die-cut", width_mm: 62, length_mm: 29, two_color: false },
  ],
  configured: "62",
  loaded: "62red",
};

const ok = (data) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });

function routes({ print }) {
  return (url) => {
    if (url === "/api/labels/tapes") return ok(TAPES);
    if (url === "/api/labels/locations/print") return print();
    return ok({});
  };
}

async function open(document, fetchImpl) {
  const page = loadPage(FIXTURE, SCRIPTS, { fetchImpl });
  page.window.openLabelPrintDialog({ root: 5, preview: 5, what: "3 labels" });
  await tick();
  return page;
}

describe("label_print.js", () => {
  it("offers the tapes and pre-selects the one the printer is holding", async () => {
    const { document, window } = await open(
      null,
      routes({ print: () => ok({ sent: 1, confirmed: true, tape: "62red" }) }),
    );

    const select = document.getElementById("label-print-tape");
    expect([...select.options].map((o) => o.value)).toEqual(["62", "62red", "62x29"]);
    // Not the configured "62": what is actually in the machine needs no decision.
    expect(select.value).toBe("62red");
    expect(document.getElementById("label-print-tape-hint").textContent).toContain(
      "62 mm continuous, black/red",
    );
    // The preview shows the bitmap for that tape, before anything is printed.
    expect(document.getElementById("label-print-preview").src).toContain(
      "/api/labels/locations/5/preview.png?tape=62red",
    );
    expect(window.openLabelPrintDialog).toBeTypeOf("function");
  });

  it("prints the chosen tape and reports what the printer confirmed", async () => {
    const { document, fetchMock } = await open(
      null,
      routes({ print: () => ok({ sent: 3, confirmed: true, tape: "62red" }) }),
    );

    document.getElementById("label-print-form").dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    const [url, opts] = fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/labels/locations/print");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(JSON.parse(opts.body)).toEqual({
      tape: "62red",
      accept_loaded: false,
      root: 5,
    });
    // "Printed", because the printer said so — see the toast wording rule.
    expect(document.querySelector(".toast").textContent).toContain("Printed 3 labels");
    expect(document.getElementById("label-print-dialog").close).toHaveBeenCalled();
  });

  it("says a job was only sent when the printer did not confirm it", async () => {
    const { document } = await open(
      null,
      routes({ print: () => ok({ sent: 1, confirmed: false, tape: "62" }) }),
    );
    document.getElementById("label-print-form").dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    const toast = document.querySelector(".toast").textContent;
    expect(toast).toContain("Sent 1 label");
    expect(toast).not.toContain("Printed");
  });

  it("asks what to do when the printer is holding a different roll", async () => {
    let attempt = 0;
    const { document, fetchMock } = await open(
      null,
      routes({
        print: () => {
          attempt += 1;
          if (attempt === 1) {
            return Promise.resolve({
              ok: false,
              status: 409,
              json: () =>
                Promise.resolve({
                  detail: "the printer is holding 62 mm continuous tape…",
                  requested: "62x29",
                  loaded: "62",
                }),
            });
          }
          return ok({ sent: 1, confirmed: true, tape: "62" });
        },
      }),
    );

    document.getElementById("label-print-tape").value = "62x29";
    document.getElementById("label-print-form").dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    // Not an error message but a question, naming both rolls in the words the
    // picker uses — the answer is the user's, not the server's.
    const mismatch = document.getElementById("label-print-mismatch");
    expect(mismatch.hidden).toBe(false);
    expect(document.getElementById("label-print-mismatch-text").textContent).toContain(
      "62 mm continuous",
    );
    expect(document.getElementById("label-print-mismatch-text").textContent).toContain(
      "62 × 29 mm die-cut",
    );
    expect(document.getElementById("label-print-dialog").close).not.toHaveBeenCalled();

    document.getElementById("label-print-use-loaded").click();
    await tick();

    expect(JSON.parse(fetchMock.mock.calls.at(-1)[1].body).accept_loaded).toBe(true);
    expect(document.querySelector(".toast").textContent).toContain("Printed 1 label");
  });

  it("re-reads the printer when the roll has been changed", async () => {
    const { document, fetchMock } = await open(
      null,
      routes({ print: () => ok({ sent: 1, confirmed: true, tape: "62red" }) }),
    );
    const before = fetchMock.mock.calls.filter((c) => c[0] === "/api/labels/tapes").length;

    document.getElementById("label-print-recheck").click();
    await tick();
    await tick();

    const after = fetchMock.mock.calls.filter((c) => c[0] === "/api/labels/tapes").length;
    expect(after).toBe(before + 1); // everything it told us is stale
    expect(fetchMock.mock.calls.at(-1)[0]).toBe("/api/labels/locations/print");
  });
});

describe("stopping a run", () => {
  it("shows progress and a way out while the labels are going", async () => {
    // A whole cabinet is hundreds of labels; being able to say "not that" is
    // worth more than anything else on the screen while it runs.
    let release;
    const inFlight = new Promise((resolve) => {
      release = () =>
        resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sent: 3, confirmed: true, tape: "62red", stopped: true }),
        });
    });
    const { document, fetchMock } = await open(null, routes({ print: () => inFlight }));

    document.getElementById("label-print-form").dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();
    expect(document.getElementById("label-print-progress").hidden).toBe(false);

    document.getElementById("label-print-stop").click();
    await tick();
    const [url, opts] = fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/labels/stop");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);

    release();
    await tick();
    // Neither a success nor a failure: the labels that did come out are on the
    // bench and have to be accounted for.
    expect(document.querySelector(".toast").textContent).toBe("Stopped after 3 labels.");
    expect(document.getElementById("label-print-progress").hidden).toBe(true);
  });
});

describe("a dialog reopened while a run is going", () => {
  it("picks the run back up instead of losing the Stop button", async () => {
    // Escape closes the dialog; the run continues, because the request is still
    // open. Reopening is the obvious way back to the one control that matters.
    let release;
    const inFlight = new Promise((resolve) => {
      release = () =>
        resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sent: 8, confirmed: true, tape: "62red" }),
        });
    });
    const page = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: (url) => {
        if (url === "/api/labels/tapes") return ok(TAPES);
        if (url === "/api/labels/job")
          return ok({ printing: true, done: 3, total: 8 });
        if (url === "/api/labels/locations/print") return inFlight;
        return ok({});
      },
    });

    await page.window.openLabelPrintDialog({ root: 5, preview: 5, what: "8 labels" });
    page.document.getElementById("label-print-form").dispatchEvent(
      new page.document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    page.document.getElementById("label-print-dialog").close(); // Escape
    await page.window.openLabelPrintDialog({ root: 5, preview: 5, what: "8 labels" });
    await tick();

    const progress = page.document.getElementById("label-print-progress");
    expect(progress.hidden).toBe(false);
    expect(page.document.getElementById("label-print-progress-text").textContent).toContain(
      "3 of 8",
    );
    expect(page.document.getElementById("label-print-stop").disabled).toBe(false);
    release();
    await tick();
  });

  it("says how many came out when a run fails part way", async () => {
    // The failure and the stop leave the same labels on the bench; only one of
    // them used to account for them.
    const { document } = await open(
      null,
      routes({
        print: () =>
          Promise.resolve({
            ok: false,
            status: 503,
            json: () =>
              Promise.resolve({
                detail: "the printer stopped: the tape has run out",
                printed: 3,
                total: 8,
              }),
          }),
      }),
    );
    document.getElementById("label-print-form").dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();

    const error = document.getElementById("label-print-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe(
      "Printed 3 of 8, then stopped: the printer stopped: the tape has run out",
    );
  });
});

describe("printing right after creating a location", () => {
  const DIALOG = `
    <dialog id="location-dialog">
      <form id="location-form">
        <select name="type"><option value="drawer">drawer</option></select>
        <input name="name" />
        <select name="parent_id"><option value="">None</option></select>
        <label class="check"><input type="checkbox" id="location-print-label" /></label>
        <p id="location-error" hidden></p>
        <button type="submit" id="location-submit">Create</button>
      </form>
    </dialog>`;

  function created(printImpl) {
    return (url, opts) => {
      if (url === "/api/locations") return ok({ id: 7, name: "D1" });
      if (url === "/api/labels/locations/print") return printImpl(url, opts);
      return ok({});
    };
  }

  async function submit(fetchImpl) {
    const page = loadPage(DIALOG, ["shared.js", "location_dialog.js"], { fetchImpl });
    page.window.openLocationDialog(() => {});
    page.document.querySelector('[name="name"]').value = "D1";
    page.document.getElementById("location-print-label").checked = true;
    page.document.getElementById("location-form").dispatchEvent(
      new page.document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();
    await tick();
    return page;
  }

  it("prints the new location's label and says so after the reload", async () => {
    const { window, fetchMock } = await submit(
      created(() => ok({ sent: 1, confirmed: true, tape: "62red" })),
    );

    const [url, opts] = fetchMock.mock.calls.at(-1);
    expect(url).toBe("/api/labels/locations/print");
    // No tape named: whatever roll is loaded is the right one here, and asking
    // belongs to the print dialog, not to creating a shelf.
    expect(JSON.parse(opts.body)).toEqual({ ids: [7] });
    // The page reloads immediately, so the message has to outlive it.
    const pending = JSON.parse(window.sessionStorage.getItem("shelfos:pending-toast"));
    expect(pending.message).toContain("Label printed");
  });

  it("keeps a failed print from making a saved location look unsaved", async () => {
    const { document, window } = await submit(
      created(() =>
        Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ detail: "the printer is not there" }),
        }),
      ),
    );

    // The dialog closed and no error is shown on the form: the location saved.
    expect(document.getElementById("location-error").hidden).toBe(true);
    expect(document.getElementById("location-dialog").close).toHaveBeenCalled();
    const pending = JSON.parse(window.sessionStorage.getItem("shelfos:pending-toast"));
    expect(pending.message).toContain("was created, but its label did not print");
    expect(pending.message).toContain("the printer is not there");
  });
});

describe("guards that only matter when something goes wrong", () => {
  it("a double-click prints one job, not two", async () => {
    // Unlike a double-POST elsewhere in this app, the second one cannot be
    // undone by reloading: the server serialises prints, so it does not
    // collide — it waits its turn and prints everything again, on real tape.
    let release;
    const inFlight = new Promise((resolve) => {
      release = () => resolve({ ok: true, status: 200, json: () => Promise.resolve({ sent: 1, confirmed: true, tape: "62red" }) });
    });
    const { document, fetchMock } = await open(
      null,
      routes({ print: () => inFlight }),
    );

    const submit = () =>
      document.getElementById("label-print-form").dispatchEvent(
        new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
      );
    submit();
    submit();
    await tick();

    const prints = fetchMock.mock.calls.filter(
      (call) => call[0] === "/api/labels/locations/print",
    );
    expect(prints).toHaveLength(1);
    expect(document.getElementById("label-print-submit").disabled).toBe(true);

    release();
    await tick();
    expect(document.getElementById("label-print-submit").disabled).toBe(false);
  });

  it("a message left for after a reload is shown on the next page", () => {
    // The whole reason shared.js grew this: "created, but the label did not
    // print" is reported by a page that is about to be replaced.
    const session = {
      "shelfos:pending-toast": JSON.stringify({
        message: "Label sent to the printer.",
        options: { tone: "ok" },
      }),
    };
    const { document, window } = loadPage("<div></div>", ["shared.js"], {
      sessionStorage: session,
    });

    const toast = document.querySelector(".toast");
    expect(toast).not.toBeNull();
    expect(toast.textContent).toBe("Label sent to the printer.");
    expect(toast.className).toContain("toast-ok");
    // Taken, not left to reappear on every page for the rest of the session.
    expect(window.sessionStorage.getItem("shelfos:pending-toast")).toBeNull();
  });

  it("editing a location does not offer to reprint its label", async () => {
    const page = loadPage(
      `<dialog id="location-dialog"><form id="location-form">
         <select name="type"><option value="drawer">drawer</option></select>
         <input name="name" /><select name="parent_id"><option value="">None</option></select>
         <label class="check" id="location-print-row">
           <input type="checkbox" id="location-print-label" /></label>
         <p id="location-error" hidden></p>
       </form></dialog>`,
      ["shared.js", "location_dialog.js"],
    );

    page.window.openLocationDialog(() => {}, { id: 3, name: "D1", type: "drawer" });
    expect(page.document.getElementById("location-print-row").hidden).toBe(true);

    page.window.openLocationDialog(() => {});
    expect(page.document.getElementById("location-print-row").hidden).toBe(false);
  });

  it("and does not reprint even if the hidden box is ticked", async () => {
    // Hiding the offer and refusing to act on it are two guards, and only the
    // second one survives a stray click, a restored form, or a future layout.
    const page = loadPage(
      `<dialog id="location-dialog"><form id="location-form">
         <select name="type"><option value="drawer">drawer</option></select>
         <input name="name" /><select name="parent_id"><option value="">None</option></select>
         <label class="check" id="location-print-row">
           <input type="checkbox" id="location-print-label" /></label>
         <p id="location-error" hidden></p>
       </form></dialog>`,
      ["shared.js", "location_dialog.js"],
      { fetchImpl: (url) => ok(url === "/api/locations/3" ? { id: 3 } : {}) },
    );

    page.window.openLocationDialog(() => {}, { id: 3, name: "D1", type: "drawer" });
    page.document.getElementById("location-print-label").checked = true;
    page.document.querySelector('[name="name"]').value = "D1 renamed";
    page.document.getElementById("location-form").dispatchEvent(
      new page.document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
    await tick();
    await tick();

    const printed = page.fetchMock.mock.calls.filter(
      (call) => call[0] === "/api/labels/locations/print",
    );
    expect(printed).toHaveLength(0);
  });
});

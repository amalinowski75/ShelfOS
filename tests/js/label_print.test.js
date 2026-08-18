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

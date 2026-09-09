import { describe, it, expect } from "vitest";
import { loadPage, tick } from "./harness.js";

const SCRIPTS = ["label_printer.js"];

// The parts of label_printer.html the script touches.
const FIXTURE = `
  <form id="setup-form">
    <input id="bridge_port" name="bridge_port" value="9100" />
  </form>
  <input id="probe-device" value="tcp://127.0.0.1:9100" readonly />
  <button type="button" id="probe-btn">Test connection</button>
  <p id="probe-result" hidden></p>`;

const ok = (data) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
const refused = (detail) =>
  Promise.resolve({ ok: false, status: 422, json: () => Promise.resolve({ detail }) });

function open(fetchImpl) {
  return loadPage(FIXTURE, SCRIPTS, { fetchImpl });
}

async function press(page) {
  page.document.getElementById("probe-btn").click();
  await tick();
  return page.document.getElementById("probe-result");
}

describe("label_printer.js", () => {
  it("keeps the address to test in step with the port field", async () => {
    const { document, window } = open();
    const port = document.getElementById("bridge_port");
    port.value = "9223";
    port.dispatchEvent(new window.Event("input"));
    expect(document.getElementById("probe-device").value).toBe("tcp://127.0.0.1:9223");
  });

  it("tests the address shown, and says what the printer holds", async () => {
    const calls = [];
    const page = open((url) => {
      calls.push(url);
      return ok({
        answered: true,
        busy: false,
        detail: "the printer answered: it is holding 62 tape",
        tape: "62",
        errors: [],
      });
    });
    const result = await press(page);
    expect(calls[0]).toContain("device=tcp%3A%2F%2F127.0.0.1%3A9100");
    expect(result.hidden).toBe(false);
    expect(result.textContent).toContain("62 tape");
    expect(result.dataset.state).toBe("good");
  });

  it("reads a fault as a failure even though the printer answered", async () => {
    // answered: true is not success — the printer said what is wrong with it.
    const page = open(() =>
      ok({ answered: true, busy: false, detail: "the printer reports: no media", errors: ["no media"] }),
    );
    const result = await press(page);
    expect(result.dataset.state).toBe("bad");
    expect(result.textContent).toContain("no media");
  });

  it("shows the server's refusal rather than a status code", async () => {
    const page = open(() => refused("only the server's own loopback can be tested"));
    const result = await press(page);
    expect(result.dataset.state).toBe("bad");
    expect(result.textContent).toContain("loopback");
  });

  it("re-enables the button after a network failure", async () => {
    const page = open(() => Promise.reject(new Error("offline")));
    const result = await press(page);
    expect(result.dataset.state).toBe("bad");
    expect(page.document.getElementById("probe-btn").disabled).toBe(false);
  });

  it("asks for a port before asking the printer anything", async () => {
    const calls = [];
    const page = open((url) => {
      calls.push(url);
      return ok({});
    });
    page.document.getElementById("bridge_port").value = "";
    page.document
      .getElementById("bridge_port")
      .dispatchEvent(new page.window.Event("input"));
    const result = await press(page);
    expect(calls).toEqual([]);
    expect(result.textContent).toContain("port");
  });
});

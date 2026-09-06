import { describe, it, expect, vi } from "vitest";
import {
  loadPage,
  tick,
  CSRF,
  bomTakeFixture,
  bomTakeUndoFixture,
} from "./harness.js";

// The order bom_report.html loads them in, not a convenient one: bom_take.js runs
// BEFORE boms_report.js there, and a test that reversed the two hid a dialog that
// never wired itself up at all.
const SCRIPTS = ["shared.js", "location_tree.js", "bom_take.js", "boms_report.js"];

// A plan the server would answer with: one line, satisfied from the gathering
// drawer.
function plan(overrides = {}) {
  return {
    bom_id: 7,
    bom_name: "Kontroler CNC",
    boards: 1,
    source_location_id: 4,
    source_path: "Kontroler CNC",
    can_run: true,
    blocked_references: [],
    unanswered_references: [],
    total_shortfall: 0,
    lines: [
      {
        line_id: 11,
        references: "R1,R2",
        mpn: "RES-1K",
        component_id: 3,
        per_board: 2,
        requested: 2,
        shortfall: 0,
        needs_choice: false,
        blocked: null,
        sources: [
          {
            location_id: 5,
            path: "Kontroler CNC / Rezystory",
            available: 50,
            quantity: 2,
            inside: true,
          },
        ],
        candidates: [],
      },
    ],
    ...overrides,
  };
}

// Answering the report feed and the preview from one stub: the page loads its
// report first, and the dialog then asks for a plan.
function server(planBody, { onTake } = {}) {
  const calls = [];
  const impl = (url, opts) => {
    calls.push([url, opts?.method || "GET", opts?.body && JSON.parse(opts.body)]);
    if (url.includes("/take/preview")) {
      return Promise.resolve({ ok: true, json: async () => planBody() });
    }
    if (url.endsWith("/takes")) {
      return onTake
        ? onTake()
        : Promise.resolve({ ok: true, json: async () => ({ id: 99 }) });
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({ summary: { unresolved: 0 }, lines: [] }),
    });
  };
  return { impl, calls };
}

function pickGathering(document) {
  document.querySelector(".loc-picker-node").click();
}

describe("bom_take.js — the preview", () => {
  it("asks for nothing until a gathering location is picked", async () => {
    const { impl, calls } = server(plan);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    await tick();

    expect(calls.some((c) => c[0].includes("/take/preview"))).toBe(false);
    expect(document.getElementById("take-confirm").disabled).toBe(true);
    expect(document.getElementById("take-summary").textContent).toContain(
      "Pick where",
    );
  });

  it("prefills the board count from the report and sends it", async () => {
    const { impl, calls } = server(plan);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-boards").value = "5";
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    expect(document.getElementById("take-boards").value).toBe("5");
    const preview = calls.find((c) => c[0].includes("/take/preview"));
    expect(preview[2]).toMatchObject({ boards: 5, source_location_id: 4 });
  });

  it("renders each line with an editable quantity and where it comes from", async () => {
    const { impl } = server(plan);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    const row = document.querySelector("#take-rows tr");
    expect(row.textContent).toContain("R1,R2");
    expect(row.querySelector(".take-qty").value).toBe("2");
    expect(row.textContent).toContain("Kontroler CNC / Rezystory ×2");
    expect(document.getElementById("take-confirm").disabled).toBe(false);
  });

  it("re-asks the server once for a burst of typing, with the override", async () => {
    const { impl, calls } = server(plan);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();
    const before = calls.filter((c) => c[0].includes("/take/preview")).length;

    const qty = document.querySelector(".take-qty");
    for (const value of ["1", "12", "120"]) {
      qty.value = value;
      qty.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    }
    await new Promise((r) => setTimeout(r, 350));
    await tick();

    const previews = calls.filter((c) => c[0].includes("/take/preview"));
    expect(previews.length).toBe(before + 1); // one request, not three
    expect(previews.at(-1)[2].lines).toEqual([
      { line_id: 11, quantity: 120, source_location_id: null },
    ]);
  });

  it("keeps the caret in the field being edited when the plan comes back", async () => {
    // A 300ms pause between two digits is ordinary; rebuilding the tbody replaces
    // the very input the caret is in, and the rest of the number goes nowhere.
    const { impl } = server(() => plan({ lines: [{ ...plan().lines[0], requested: 12 }] }));
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    const qty = document.querySelector(".take-qty");
    qty.focus();
    qty.value = "12";
    qty.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 350));
    await tick();

    // A NEW element — the table was rebuilt — but it is the one holding focus.
    const after = document.querySelector(".take-qty");
    expect(document.activeElement).toBe(after);
    expect(after.value).toBe("12");
  });

  it("treats a cleared field as mid-edit, not as a request for zero", async () => {
    const { impl, calls } = server(plan);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();
    const before = calls.filter((c) => c[0].includes("/take/preview")).length;

    const qty = document.querySelector(".take-qty");
    qty.value = "";
    qty.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 350));
    await tick();

    // No replan, so nothing stamps a "0" back into the box they just cleared.
    expect(calls.filter((c) => c[0].includes("/take/preview")).length).toBe(before);
    expect(document.querySelector(".take-qty").value).toBe("");
  });

  it("does not let a stale plan overwrite a newer one", async () => {
    // Two previews in flight; the first to be asked for answers last.
    let resolveFirst;
    let first = true;
    const impl = (url) => {
      if (!url.includes("/take/preview")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ summary: { unresolved: 0 }, lines: [] }),
        });
      }
      if (first) {
        first = false;
        return new Promise((resolve) => {
          resolveFirst = () =>
            resolve({ ok: true, json: async () => plan({ total_shortfall: 999 }) });
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => plan({ total_shortfall: 7 }),
      });
    };
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document); // first preview, left hanging
    pickGathering(document); // second preview, answers immediately
    await tick();
    resolveFirst();
    await tick();

    expect(document.getElementById("take-summary").textContent).toContain("7 part(s)");
  });

  it("asks where to take a line stocked in several places, and blocks until told", async () => {
    const asking = () =>
      plan({
        can_run: false,
        unanswered_references: ["R1,R2"],
        lines: [
          {
            ...plan().lines[0],
            needs_choice: true,
            sources: [],
            candidates: [
              { location_id: 8, path: "Regal A", available: 50, quantity: 0, inside: false },
              { location_id: 9, path: "Regal B", available: 20, quantity: 0, inside: false },
            ],
          },
        ],
      });
    const { impl, calls } = server(asking);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    const select = document.querySelector(".take-choice");
    expect([...select.options].map((o) => o.value)).toEqual(["", "8", "9"]);
    expect(document.getElementById("take-confirm").disabled).toBe(true);

    select.value = "9";
    select.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
    await tick();

    expect(calls.at(-1)[2].lines).toEqual([
      { line_id: 11, quantity: null, source_location_id: 9 },
    ]);
  });

  it("names the lines that stop the run and refuses to confirm", async () => {
    const blocked = () =>
      plan({
        can_run: false,
        blocked_references: ["U7", "U8"],
        lines: [{ ...plan().lines[0], blocked: "unassigned", sources: [] }],
      });
    const { impl } = server(blocked);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    expect(document.getElementById("take-blockers").hidden).toBe(false);
    expect(document.getElementById("take-blockers-text").textContent).toContain("U7, U8");
    expect(document.getElementById("take-confirm").disabled).toBe(true);
  });

  it("says how short the run will be", async () => {
    const short = () =>
      plan({
        total_shortfall: 40,
        lines: [{ ...plan().lines[0], shortfall: 40 }],
      });
    const { impl } = server(short);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    expect(document.getElementById("take-summary").textContent).toContain("40 part(s)");
    expect(document.querySelector("#take-rows .badge").textContent).toContain("40");
  });

  it("escapes the references, MPN and location paths", async () => {
    // All three come from an uploaded CSV or a free-text location name.
    const nasty = () =>
      plan({
        lines: [
          {
            ...plan().lines[0],
            references: "<img src=x onerror=1>",
            mpn: "<script>bad()</script>",
            sources: [
              {
                location_id: 5,
                path: "<b>Regal</b>",
                available: 5,
                quantity: 2,
                inside: true,
              },
            ],
          },
        ],
      });
    const { impl } = server(nasty);
    const { document } = loadPage(bomTakeFixture(), SCRIPTS, { fetchImpl: impl });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();

    // Asserted against the DOM, not innerHTML: the HTML serializer leaves "<"
    // alone inside an attribute value, so a string check there passes on an
    // escaped aria-label and proves nothing either way.
    expect(document.querySelectorAll("#take-rows img, #take-rows script, #take-rows b"))
      .toHaveLength(0);
    const cells = document.querySelectorAll("#take-rows td");
    expect(cells[0].textContent).toBe("<img src=x onerror=1>");
    expect(cells[1].textContent).toBe("<script>bad()</script>");
    expect(cells[3].textContent.trim()).toBe("<b>Regal</b> ×2");
  });
});

describe("bom_take.js — confirming", () => {
  it("posts the answers and goes to the snapshot", async () => {
    const { impl, calls } = server(plan);
    const { document, navigations } = loadPage(bomTakeFixture(), SCRIPTS, {
      fetchImpl: impl,
    });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();
    document.getElementById("take-confirm").click();
    await tick();

    const post = calls.find((c) => c[0] === "/api/boms/7/takes");
    expect(post[1]).toBe("POST");
    expect(post[2]).toMatchObject({ boards: 1, source_location_id: 4 });
    // jsdom reports only THAT a navigation was attempted, never where to — so
    // the destination is pinned as a value, and the handler is shown to route
    // through the function that builds it.
    expect(navigations).not.toHaveLength(0);
  });

  it("builds the snapshot URL, and goes through that function to get there", async () => {
    const { impl } = server(plan);
    const { window, document, navigations } = loadPage(bomTakeFixture(), SCRIPTS, {
      fetchImpl: impl,
    });
    expect(window.takeSnapshotUrl(99)).toBe("/bom-takes/99");

    const spy = vi.fn(() => "/somewhere-else");
    window.takeSnapshotUrl = spy;
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();
    document.getElementById("take-confirm").click();
    await tick();

    expect(spy).toHaveBeenCalledWith(99);
    expect(navigations).not.toHaveLength(0);
  });

  it("sends the CSRF token", async () => {
    const { impl } = server(plan);
    const { document, fetchMock } = loadPage(bomTakeFixture(), SCRIPTS, {
      fetchImpl: impl,
    });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();
    document.getElementById("take-confirm").click();
    await tick();

    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/boms/7/takes");
    expect(post[1].headers["X-CSRF-Token"]).toBe(CSRF);
  });

  it("keeps the dialog open on a refusal and says why", async () => {
    const { impl } = server(plan, {
      onTake: () =>
        Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({ detail: "only 3 in stock at that location" }),
        }),
    });
    const { document, navigations } = loadPage(bomTakeFixture(), SCRIPTS, {
      fetchImpl: impl,
    });
    document.getElementById("bom-take").click();
    pickGathering(document);
    await tick();
    document.getElementById("take-confirm").click();
    await tick();

    expect(document.getElementById("take-error-row").hidden).toBe(false);
    expect(document.getElementById("take-error").textContent).toContain("only 3");
    expect(document.getElementById("take-confirm").disabled).toBe(false);
    expect(navigations).toHaveLength(0);
  });
});

describe("bom_take_undo.js", () => {
  const UNDO = ["shared.js", "bom_take_undo.js"];

  it("will not confirm without a reason", () => {
    const { document } = loadPage(bomTakeUndoFixture(), UNDO);
    document.getElementById("take-undo").click();
    const confirm = document.getElementById("take-undo-confirm");
    expect(confirm.disabled).toBe(true);

    const reason = document.getElementById("take-undo-reason");
    reason.value = "   ";
    reason.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    expect(confirm.disabled).toBe(true); // whitespace is not a reason

    reason.value = "board scrapped";
    reason.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    expect(confirm.disabled).toBe(false);
  });

  it("sends the reason verbatim and reloads", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) }));
    const { document, navigations } = loadPage(bomTakeUndoFixture(), UNDO, {
      fetchImpl,
    });
    document.getElementById("take-undo").click();
    const reason = document.getElementById("take-undo-reason");
    reason.value = "  board scrapped  ";
    reason.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    document.getElementById("take-undo-confirm").click();
    await tick();

    const [url, opts] = fetchImpl.mock.calls.at(-1);
    expect(url).toBe("/api/bom-takes/12/reverse");
    expect(JSON.parse(opts.body)).toEqual({ reason: "  board scrapped  " });
    // jsdom cannot reload either; the attempt is what it reports. Trimming is the
    // server's job — the page must not decide what the reason "really" says.
    expect(navigations).not.toHaveLength(0);
  });

  it("shows a refusal without closing the dialog", async () => {
    const fetchImpl = () =>
      Promise.resolve({
        ok: false,
        status: 422,
        json: async () => ({ detail: "this take has already been reversed" }),
      });
    const { document } = loadPage(bomTakeUndoFixture(), UNDO, { fetchImpl });
    document.getElementById("take-undo").click();
    const reason = document.getElementById("take-undo-reason");
    reason.value = "scrapped";
    reason.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
    document.getElementById("take-undo-confirm").click();
    await tick();

    expect(document.getElementById("take-undo-error").textContent).toContain(
      "already been reversed",
    );
    expect(document.getElementById("take-undo-error-row").hidden).toBe(false);
  });
});

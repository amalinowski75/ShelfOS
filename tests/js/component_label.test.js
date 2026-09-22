import { describe, it, expect, vi } from "vitest";
import { loadPage } from "./harness.js";

// The component detail page's Print label button. It owns one decision — which
// part the dialog is about — so that is what is pinned here; everything after
// the call belongs to label_print.js and is tested there.
const FIXTURE = `
  <div class="head"><div><h1>STM32F103C8T6</h1></div></div>
  <button id="component-print-label-btn" data-component-id="7">Print label</button>`;

function open(fixture = FIXTURE) {
  const page = loadPage(fixture, ["shared.js", "component_label.js"]);
  page.window.openLabelPrintDialog = vi.fn();
  return page;
}

describe("component_label.js", () => {
  it("asks for this component's label, named as the page names it", () => {
    const page = open();
    page.document.getElementById("component-print-label-btn").click();

    expect(page.window.openLabelPrintDialog).toHaveBeenCalledWith({
      kind: "components",
      ids: [7],
      preview: 7,
      what: "One label: “STM32F103C8T6”",
    });
  });

  it("does nothing at all where there is no button", () => {
    // No printer set up, or a reader who may not print: the script still loads
    // on the page, and must not throw on the way past.
    expect(() =>
      open(`<div class="head"><div><h1>NE555P</h1></div></div>`),
    ).not.toThrow();
  });
});

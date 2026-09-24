// The palette in app.css, read as text: jsdom cannot evaluate light-dark(), so
// these check the declarations themselves.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";

const CSS = readFileSync(new URL("../../app/web/static/app.css", import.meta.url), "utf8");

// The body of the first rule whose selector text starts at `start`.
function block(start) {
  const at = CSS.indexOf(start);
  expect(at, `${start} not found`).toBeGreaterThanOrEqual(0);
  const open = CSS.indexOf("{", at + start.length - 1);
  return CSS.slice(open + 1, CSS.indexOf("\n}", open));
}

function declarations(body) {
  const out = {};
  const noComments = body.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const m of noComments.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    out[m[1]] = m[2].replace(/\s+/g, " ").trim();
  }
  return out;
}

const root = declarations(block(":root {"));
const themed = Object.keys(root).filter((name) => root[name].includes("light-dark("));
const dim = declarations(block(':root[data-theme="dim"] {'));

describe("the dim palette", () => {
  it("sets every themed token, so none falls through to Dark", () => {
    expect(Object.keys(dim).sort()).toEqual([...themed].sort());
  });

  // WCAG 2 contrast. A translucent fill is flattened onto --surface, which is
  // where the badges and the unfilled fields sit.
  const rgb = (value) => {
    const hex = value.match(/^#([0-9a-f]{6})$/i);
    if (hex) return [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16));
    const [r, g, b, a] = value.match(/[\d.]+/g).map(Number);
    const ground = rgb(dim["--surface"]);
    return [r, g, b].map((c, i) => Math.round(a * c + (1 - a) * ground[i]));
  };
  const luminance = (c) =>
    c
      .map((x) => x / 255)
      .map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4))
      .reduce((sum, x, i) => sum + x * [0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (fg, bg) => {
    const [a, b] = [luminance(rgb(dim[fg])), luminance(rgb(dim[bg]))];
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };

  const PAIRS = [
    ...["--text", "--muted", "--accent", "--accent-strong", "--ok", "--warn", "--danger", "--neutral"]
      .flatMap((fg) => [[fg, "--surface"], [fg, "--surface-2"], [fg, "--bg"]]),
    ["--text", "--row-picked"],
    ["--text", "--row-hover"],
    ["--text", "--unfilled-tint"],
    ["--on-accent", "--accent"],
    ["--accent-ink", "--accent-soft"],
    ["--ok", "--ok-soft"],
    ["--warn", "--warn-soft"],
    ["--danger", "--danger-soft"],
    ["--neutral", "--neutral-soft"],
  ];

  it.each(PAIRS)("%s on %s reaches 4.5:1", (fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps --unfilled-tint equal to --danger-soft flattened onto --surface", () => {
    expect(rgb(dim["--unfilled-tint"])).toEqual(rgb(dim["--danger-soft"]));
  });
});

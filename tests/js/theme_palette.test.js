// The palette in app.css, read as text: jsdom cannot evaluate light-dark() or
// @supports, so these check the declarations themselves.

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

// One side of every light-dark(a, b) in a value (0 = light, 1 = dark).
function side(value, which) {
  let out = "";
  let i = 0;
  for (;;) {
    const j = value.indexOf("light-dark(", i);
    if (j < 0) return out + value.slice(i);
    out += value.slice(i, j);
    let k = j + "light-dark(".length;
    let depth = 1;
    const args = [""];
    for (; ; k++) {
      const ch = value[k];
      if (ch === "(") depth++;
      if (ch === ")" && --depth === 0) break;
      if (ch === "," && depth === 1) args.push("");
      else args[args.length - 1] += ch;
    }
    out += args[which].trim();
    i = k + 1;
  }
}

// A shadow list with the layers the other theme owns (coloured transparent) dropped.
const withoutClearLayers = (value) =>
  value
    .split(/,(?![^(]*\))/)
    .map((layer) => layer.trim())
    .filter((layer) => !layer.endsWith("transparent"))
    .join(", ");

const root = declarations(block(":root {"));
const themed = Object.keys(root).filter((name) => root[name].includes("light-dark("));
const fallback = declarations(block("@supports not (color: light-dark(#000, #fff)) {\n  :root {"));
// The themes that set their own palette rather than picking a side of :root.
const PALETTES = Object.fromEntries(
  ["dim", "warm"].map((name) => [name, declarations(block(`:root[data-theme="${name}"] {`))]),
);

describe("the fallback for browsers without light-dark()", () => {
  it("sets every themed token, and only those", () => {
    expect(Object.keys(fallback).sort()).toEqual([...themed].sort());
  });

  it("is the light side of :root, value for value", () => {
    for (const name of themed) {
      expect(fallback[name], name).toBe(withoutClearLayers(side(root[name], 0)));
    }
  });
});

describe.each(Object.keys(PALETTES))("the %s palette", (name) => {
  const palette = PALETTES[name];

  it("sets every themed token, so none falls through to Light or Dark", () => {
    expect(Object.keys(palette).sort()).toEqual([...themed].sort());
  });

  // WCAG 2 contrast. A translucent fill is flattened onto --surface, which is
  // where the badges and the unfilled fields sit.
  const rgb = (value) => {
    const hex = value.match(/^#([0-9a-f]{6})$/i);
    if (hex) return [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16));
    const [r, g, b, a] = value.match(/[\d.]+/g).map(Number);
    const ground = rgb(palette["--surface"]);
    return [r, g, b].map((c, i) => Math.round(a * c + (1 - a) * ground[i]));
  };
  const luminance = (c) =>
    c
      .map((x) => x / 255)
      .map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4))
      .reduce((sum, x, i) => sum + x * [0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (fg, bg) => {
    const [a, b] = [luminance(rgb(palette[fg])), luminance(rgb(palette[bg]))];
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
    expect(rgb(palette["--unfilled-tint"])).toEqual(rgb(palette["--danger-soft"]));
  });
});

describe("the header logo", () => {
  // The logo's lettering has fixed colours: a theme with a dark ground needs the
  // light-lettered logo-dark.svg, or the name is dark grey on near-black.
  const darkThemes = [...CSS.matchAll(/:root\[data-theme="(\w+)"\]\s*\{([^}]*)\}/g)]
    .filter(([, , body]) => /color-scheme:\s*dark\s*;/.test(body))
    .map(([, name]) => name)
    .sort();

  // The body of the at-rule block that opens with `head`, braces matched.
  function atBlock(head) {
    const at = CSS.indexOf(head);
    expect(at, `${head} not found`).toBeGreaterThanOrEqual(0);
    const open = CSS.indexOf("{", at);
    let depth = 1;
    let k = open + 1;
    for (; depth > 0; k++) {
      if (CSS[k] === "{") depth++;
      if (CSS[k] === "}") depth--;
    }
    return CSS.slice(open + 1, k - 1);
  }

  // selector -> its display value, for every innermost rule in `text`.
  function displays(text) {
    const out = {};
    const noComments = text.replace(/\/\*[\s\S]*?\*\//g, "");
    for (const [, selectors, body] of noComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const display = body.match(/display:\s*([\w-]+)/);
      if (!display) continue;
      for (const selector of selectors.split(",")) {
        out[selector.replace(/\s+/g, " ").trim()] = display[1];
      }
    }
    return out;
  }

  const supported = atBlock("@supports (color: light-dark(#000, #fff))");

  it("shows the light-ground version by default", () => {
    const all = displays(CSS);
    expect(all[".brand .logo"]).toBe("block");
    expect(all[".brand .logo-dark"]).toBe("none");
  });

  it.each(["dark", "dim"])("swaps to the dark-ground version under %s", (theme) => {
    expect(darkThemes).toContain(theme);
    const rules = displays(supported);
    expect(rules[`:root[data-theme="${theme}"] .brand .logo-light`]).toBe("none");
    expect(rules[`:root[data-theme="${theme}"] .brand .logo-dark`]).toBe("block");
  });

  it("swaps under System when the OS is dark", () => {
    const rules = displays(atBlock("@media (prefers-color-scheme: dark)"));
    expect(rules[":root:not([data-theme]) .brand .logo-light"]).toBe("none");
    expect(rules[":root:not([data-theme]) .brand .logo-dark"]).toBe("block");
    expect(supported).toContain("@media (prefers-color-scheme: dark)");
  });

  it("swaps in every dark theme and in no light one", () => {
    const swapped = (logo, display) =>
      Object.entries(displays(supported))
        .filter(([selector, value]) => selector.endsWith(` .brand .${logo}`) && value === display)
        .map(([selector]) => selector.match(/data-theme="(\w+)"/)?.[1])
        .filter(Boolean)
        .sort();
    expect(swapped("logo-dark", "block")).toEqual(darkThemes);
    expect(swapped("logo-light", "none")).toEqual(darkThemes);
  });
});

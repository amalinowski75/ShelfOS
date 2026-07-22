import { describe, it, expect } from "vitest";
import { loadPage } from "./harness.js";

const SCRIPTS = ["shared.js", "locations.js"];

// Mirrors what locations.html actually emits — which is the whole point of these
// helpers. In particular an OCCUPIED location is always expandable (it has a parts
// list even with no child locations), and it carries a count badge; a fixture that
// modelled occupied nodes as bare leaves would hide every bug in the interaction
// between the filters and the rendered contents.
//
// Lab (empty) > Rack A (empty) > D5 (occupied), D1 (empty)
// Store (empty) > D9 (empty)
// Bench (occupied, no child locations)
// Attic (occupied) > A1 (empty)   — occupied AND a path, the awkward case
function treeFixture() {
  const row = (name, { caret, occupied }) =>
    `<div class="loc-row">${
      caret
        ? `<button type="button" class="tree-caret" aria-expanded="false"></button>`
        : `<span class="tree-caret-spacer"></span>`
    }<span class="loc-name">${name}</span>${
      occupied ? `<span class="badge b-accent loc-count">1 part</span>` : ""
    }</div>`;
  const parts = (name) =>
    `<ul class="loc-parts"><li>
       <a class="cell-mpn" href="/components/1">RC0603-${name}</a>
     </li></ul>`;
  // `occupied` and `children` are independent: a location can hold stock, hold
  // sub-locations, both, or neither.
  const node = (name, occupied, children = "") => {
    const expandable = occupied || children;
    const inner =
      (occupied ? parts(name) : "") +
      (children ? `<ul class="loc-tree">${children}</ul>` : "");
    return `<li class="loc-item" data-occupied="${occupied}" data-name="${name}">
       ${row(name, { caret: expandable, occupied })}
       ${expandable ? `<div class="tree-children" hidden>${inner}</div>` : ""}
     </li>`;
  };
  return `
    <label class="check"><input type="checkbox" id="show-empty" checked /></label>
    <label class="check"><input type="checkbox" id="show-occupied" checked /></label>
    <ul class="loc-tree loc-tree-root" id="location-tree">
      ${node("Lab", false, node("Rack A", false, node("D5", true) + node("D1", false)))}
      ${node("Store", false, node("D9", false))}
      ${node("Bench", true)}
      ${node("Attic", true, node("A1", false))}
    </ul>
    <p class="empty" id="location-tree-empty" hidden></p>`;
}

function open(fixture = treeFixture()) {
  const handles = loadPage(fixture, SCRIPTS);
  const { document } = handles;
  handles.node = (name) => document.querySelector(`[data-name="${name}"]`);
  handles.visible = (name) => !handles.node(name).hidden;
  handles.expanded = (name) =>
    handles.node(name).querySelector(":scope > .tree-children")?.hidden === false;
  // "Can I actually read this location's contents?" — present, its own list not
  // hidden, and no collapsed branch between it and the screen.
  handles.partsVisible = (name) => {
    const item = handles.node(name);
    const parts = item.querySelector(":scope > .tree-children > .loc-parts");
    return !!parts && !parts.hidden && handles.expanded(name) && !item.hidden;
  };
  handles.countVisible = (name) =>
    !handles.node(name).querySelector(":scope > .loc-row > .loc-count")?.hidden;
  // One box at a time, as a user clicks them.
  handles.setFilters = ({ empty, occupied }) => {
    for (const [id, value] of [
      ["show-empty", empty],
      ["show-occupied", occupied],
    ]) {
      const box = document.getElementById(id);
      box.checked = value;
      box.dispatchEvent(new handles.window.Event("change", { bubbles: true }));
    }
  };
  // Both at once, so a test can land on a target state without passing through an
  // intermediate one that would apply its own effects on the way.
  handles.setFiltersAtOnce = ({ empty, occupied }) => {
    document.getElementById("show-empty").checked = empty;
    document.getElementById("show-occupied").checked = occupied;
    document
      .getElementById("show-empty")
      .dispatchEvent(new handles.window.Event("change", { bubbles: true }));
  };
  return handles;
}

describe("locations.js — the collapsible tree", () => {
  it("starts collapsed, so a big hierarchy isn't a wall of text", () => {
    const h = open();
    expect(h.expanded("Lab")).toBe(false);
    expect(h.expanded("Store")).toBe(false);
    // Roots are still there — it's the branches that are shut.
    expect(h.visible("Lab")).toBe(true);
    expect(h.visible("Bench")).toBe(true);
  });

  it("toggles a branch from its caret", () => {
    const h = open();
    const caret = h.node("Lab").querySelector(".tree-caret");
    caret.click();
    expect(h.expanded("Lab")).toBe(true);
    expect(caret.getAttribute("aria-expanded")).toBe("true");
    caret.click();
    expect(h.expanded("Lab")).toBe(false);
    expect(caret.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("locations.js — the empty/occupied filters", () => {
  it("shows everything with both ticked", () => {
    const h = open();
    for (const name of ["Lab", "Rack A", "D5", "D1", "Store", "D9", "Bench"]) {
      expect(h.visible(name)).toBe(true);
    }
  });

  it("leaves the user's expansion alone when both are ticked", () => {
    // "No filter" must not mean "blow the whole tree open" — that is the wall of
    // text this page is moving away from.
    const h = open();
    h.node("Lab").querySelector(".tree-caret").click();
    h.setFilters({ empty: true, occupied: true });
    expect(h.expanded("Lab")).toBe(true);
    expect(h.expanded("Store")).toBe(false);
  });

  it("occupied only: keeps ancestors as the path, greyed rather than gone", () => {
    const h = open();
    h.setFilters({ empty: false, occupied: true });

    expect(h.visible("D5")).toBe(true);
    expect(h.visible("Bench")).toBe(true);
    expect(h.visible("D1")).toBe(false); // empty leaf
    // Lab and Rack A hold nothing themselves, but they are the only way to reach
    // D5 in a collapsible tree, so they stay — marked as scaffolding, not a hit.
    for (const name of ["Lab", "Rack A"]) {
      expect(h.visible(name)).toBe(true);
      expect(h.node(name).classList.contains("is-path")).toBe(true);
    }
    expect(h.node("D5").classList.contains("is-path")).toBe(false);
    // A whole branch with nothing in it disappears rather than being greyed.
    expect(h.visible("Store")).toBe(false);
  });

  it("occupied only: opens the path so the match needs no hunting", () => {
    const h = open();
    h.setFilters({ empty: false, occupied: true });
    expect(h.expanded("Lab")).toBe(true);
    expect(h.expanded("Rack A")).toBe(true);
  });

  it("empty only: hides the occupied ones", () => {
    const h = open();
    h.setFilters({ empty: true, occupied: false });
    expect(h.visible("D1")).toBe(true);
    expect(h.visible("Store")).toBe(true);
    expect(h.visible("D9")).toBe(true);
    expect(h.visible("D5")).toBe(false);
    expect(h.visible("Bench")).toBe(false);
    // Lab is empty itself, so here it's a hit, not scaffolding.
    expect(h.node("Lab").classList.contains("is-path")).toBe(false);
  });

  it("occupied only: opens what it found, rather than showing shut drawers", () => {
    // Bench holds parts and has no child locations, so there is no "visible child"
    // to open it — but it is exactly what the user filtered for, and showing it
    // closed makes the filter useless.
    const h = open();
    h.setFilters({ empty: false, occupied: true });
    expect(h.partsVisible("Bench")).toBe(true);
    expect(h.partsVisible("D5")).toBe(true);
  });

  it("empty only: hides the contents of a location kept as the path", () => {
    // Attic holds stock (so it fails "show empty") but its empty child A1 keeps it
    // on screen. Its parts and its count badge must not ride along — they are the
    // very thing the filter was asked to hide.
    const h = open();
    h.setFilters({ empty: true, occupied: false });
    expect(h.visible("Attic")).toBe(true);
    expect(h.node("Attic").classList.contains("is-path")).toBe(true);
    expect(h.partsVisible("Attic")).toBe(false);
    expect(h.countVisible("Attic")).toBe(false);
    expect(h.visible("A1")).toBe(true);
  });

  it("gives the contents and the count back when the filter is lifted", () => {
    const h = open();
    h.setFilters({ empty: true, occupied: false });
    h.setFilters({ empty: true, occupied: true });
    expect(h.countVisible("Attic")).toBe(true);
    expect(
      h.node("Attic").querySelector(":scope > .tree-children > .loc-parts").hidden,
    ).toBe(false);
  });

  it("keeps a manually expanded branch through a trip to nothing and back", () => {
    // "Nothing matches" is a reason to show nothing, not a reason to rearrange the
    // tree: re-ticking must not leave the user's expansion silently thrown away.
    // Set both boxes together — going one at a time passes through a single-filter
    // state whose own auto-expand would mask the bug.
    const h = open();
    h.node("Store").querySelector(".tree-caret").click();
    expect(h.expanded("Store")).toBe(true);

    h.setFiltersAtOnce({ empty: false, occupied: false });
    expect(h.expanded("Store")).toBe(true); // hidden, but not collapsed

    h.setFiltersAtOnce({ empty: true, occupied: true });
    expect(h.expanded("Store")).toBe(true);
  });

  it("neither ticked: says so instead of showing a blank card", () => {
    const h = open();
    h.setFilters({ empty: false, occupied: false });
    expect(h.document.getElementById("location-tree").hidden).toBe(true);
    expect(h.document.getElementById("location-tree-empty").hidden).toBe(false);
  });

  it("restores everything when a filter is ticked back on", () => {
    const h = open();
    h.setFilters({ empty: false, occupied: false });
    h.setFilters({ empty: true, occupied: true });
    expect(h.document.getElementById("location-tree").hidden).toBe(false);
    expect(h.document.getElementById("location-tree-empty").hidden).toBe(true);
    expect(h.visible("D1")).toBe(true);
    expect(h.node("Lab").classList.contains("is-path")).toBe(false);
  });

  it("does nothing at all on a page with no tree", () => {
    // The template omits the tree entirely when there are no locations.
    expect(() => loadPage(`<p>No locations yet.</p>`, SCRIPTS)).not.toThrow();
  });
});

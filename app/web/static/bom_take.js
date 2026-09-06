// Taking a whole BOM off the shelves. The dialog only collects answers — how many
// boards, which gathering branch, an edited quantity here and there, and a choice
// when a part sits in several places outside that branch. Where the stock actually
// is, only the server knows, so every edit re-asks it for the plan rather than
// recomputing anything here.

function takeSnapshotUrl(takeId) {
  return `/bom-takes/${Number(takeId)}`;
}

const takeDialog = document.getElementById("bom-take-dialog");
if (takeDialog && typeof bomTableEl !== "undefined" && bomTableEl) {
  const takeBomId = bomTableEl.dataset.bomId;
  const boardsInput = document.getElementById("take-boards");
  const sourceInput = takeDialog.querySelector('input[name="take_source"]');
  const rows = document.getElementById("take-rows");
  const blockers = document.getElementById("take-blockers");
  const blockersText = document.getElementById("take-blockers-text");
  const confirmBtn = document.getElementById("take-confirm");
  const summary = document.getElementById("take-summary");
  const errorRow = document.getElementById("take-error-row");
  const errorText = document.getElementById("take-error");

  // The answers the user has given, keyed by BOM line id. Kept outside the table
  // because the table is rebuilt from each new plan.
  let quantities = {};
  let choices = {};
  let planToken = 0;
  let debounce = null;

  function takeBody() {
    const lines = [];
    const ids = new Set([...Object.keys(quantities), ...Object.keys(choices)]);
    for (const id of ids) {
      lines.push({
        line_id: Number(id),
        quantity: id in quantities ? quantities[id] : null,
        source_location_id: id in choices ? choices[id] : null,
      });
    }
    return {
      boards: Math.max(1, Math.floor(Number(boardsInput.value) || 1)),
      source_location_id: Number(sourceInput.value),
      lines,
    };
  }

  function sourcesCell(line) {
    if (line.needs_choice) {
      const options = line.candidates
        .map(
          (c) =>
            `<option value="${Number(c.location_id)}">${esc(c.path)} (${Number(
              c.available,
            )})</option>`,
        )
        .join("");
      // A blank first option so an unanswered question looks unanswered, rather
      // than looking like a choice nobody made on purpose.
      return (
        `<select class="control take-choice" data-line="${Number(line.line_id)}"` +
        ` aria-label="Where to take ${esc(line.references)} from">` +
        `<option value="">Choose a location…</option>${options}</select>`
      );
    }
    if (!line.sources.length) return '<span class="muted">—</span>';
    return line.sources
      .map((s) => `${esc(s.path)} ×${Number(s.quantity)}`)
      .join(" · ");
  }

  function renderPlan(plan) {
    rows.innerHTML = plan.lines
      .map(
        (line) => `<tr${line.blocked ? ' class="take-blocked"' : ""}>
          <td class="mono">${esc(line.references)}</td>
          <td class="cell-mono">${esc(line.mpn || "—")}</td>
          <td class="num"><input class="control take-qty" type="number" min="0" step="1"
              value="${Number(line.requested)}" data-line="${Number(line.line_id)}"
              aria-label="How many of ${esc(line.references)} to take"></td>
          <td>${line.blocked ? '<span class="muted">not taken</span>' : sourcesCell(line)}</td>
          <td class="num">${
            line.shortfall
              ? `<span class="badge b-warn"><span class="dot"></span>${Number(
                  line.shortfall,
                )}</span>`
              : ""
          }</td>
        </tr>`,
      )
      .join("");

    const blocked = plan.blocked_references || [];
    blockers.hidden = blocked.length === 0;
    if (blocked.length) {
      blockersText.textContent =
        `These lines do not name a component yet, so nothing can be taken for ` +
        `them: ${blocked.join(", ")}. Assign one on the report first.`;
    }
    confirmBtn.disabled = !plan.can_run;
    summary.textContent = plan.total_shortfall
      ? `${plan.total_shortfall} part(s) short of what this run wants.`
      : "";
  }

  async function refreshPlan() {
    if (!sourceInput.value) {
      rows.innerHTML = "";
      confirmBtn.disabled = true;
      summary.textContent = "Pick where the parts were gathered.";
      return;
    }
    const token = ++planToken;
    let plan;
    try {
      const resp = await fetch(`/api/boms/${takeBomId}/take/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(takeBody()),
      });
      if (!resp.ok) throw new Error(await errorMessage(resp));
      plan = await resp.json();
    } catch (err) {
      // A stale failure must not overwrite a newer answer either.
      if (token !== planToken) return;
      errorText.textContent = err.message || "Could not reach the server.";
      errorRow.hidden = false;
      confirmBtn.disabled = true;
      return;
    }
    if (token !== planToken) return; // a newer request has already answered
    errorRow.hidden = true;
    renderPlan(plan);
  }

  // One request for a burst of typing, not one per keystroke.
  function schedulePlan() {
    clearTimeout(debounce);
    debounce = setTimeout(refreshPlan, 300);
  }

  rows.addEventListener("input", (e) => {
    if (!e.target.classList.contains("take-qty")) return;
    quantities[e.target.dataset.line] = Math.max(
      0,
      Math.floor(Number(e.target.value) || 0),
    );
    schedulePlan();
  });

  rows.addEventListener("change", (e) => {
    if (!e.target.classList.contains("take-choice")) return;
    const line = e.target.dataset.line;
    if (e.target.value) choices[line] = Number(e.target.value);
    else delete choices[line];
    refreshPlan(); // a choice is one click, not a burst
  });

  boardsInput.addEventListener("change", () => {
    // A new board count replaces every quantity the user has not touched — and
    // the ones they have, too: they were answers to a different question.
    quantities = {};
    schedulePlan();
  });

  // The picker writes its hidden input and fires `change`, the way a <select>
  // would (see location_tree.js).
  sourceInput.addEventListener("change", refreshPlan);

  document.getElementById("bom-take")?.addEventListener("click", () => {
    quantities = {};
    choices = {};
    errorRow.hidden = true;
    rows.innerHTML = "";
    const onReport = document.getElementById("bom-boards");
    if (onReport) boardsInput.value = onReport.value;
    takeDialog.showModal();
    refreshPlan();
  });

  for (const button of takeDialog.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => takeDialog.close());
  }

  let taking = false;
  confirmBtn.addEventListener("click", async () => {
    if (taking) return;
    taking = true;
    confirmBtn.disabled = true;
    try {
      const resp = await fetch(`/api/boms/${takeBomId}/takes`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(takeBody()),
      });
      if (resp.ok) {
        // Straight to the snapshot: the parts are off the shelves and the record
        // of that is the next thing anyone wants to see. The URL is built by a
        // named function because jsdom cannot navigate and reports only THAT a
        // navigation was attempted, never where to — so the destination is only
        // testable as a value.
        window.location = takeSnapshotUrl((await resp.json()).id);
        return;
      }
      errorText.textContent = await errorMessage(resp);
    } catch {
      errorText.textContent = "Could not reach the server.";
    }
    errorRow.hidden = false;
    taking = false;
    confirmBtn.disabled = false;
  });
}

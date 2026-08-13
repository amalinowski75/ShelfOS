// "Generate hierarchy" dialog (spec §7), Locations page only. Levels are rows of
// (type, count, name pattern); the running total shows the multiplication before
// anything is committed, Preview does a server dry run (same validation as the
// real request — conflicts and caps surface here), Create commits and reloads.
// Uses shared.js helpers (csrfToken, errorMessage, esc).

(function () {
  const dialog = document.getElementById("bulk-location-dialog");
  if (!dialog) return; // page does not include the dialog

  const form = document.getElementById("bulk-location-form");
  const levelsEl = document.getElementById("bulk-levels");
  const template = document.getElementById("bulk-level-template");
  const totalEl = document.getElementById("bulk-total");
  const previewEl = document.getElementById("bulk-preview");
  const previewList = document.getElementById("bulk-preview-list");
  const errorEl = document.getElementById("bulk-error");
  const MAX_LEVELS = 8;

  // A level's default name pattern follows its type ("Shelf {n}") — shown as a
  // placeholder so the field documents itself and stays overridable.
  function placeholderFor(select) {
    const type = select.value;
    return `${type.charAt(0).toUpperCase()}${type.slice(1)} {n}`;
  }

  function addLevel() {
    if (levelsEl.children.length >= MAX_LEVELS) return;
    const row = template.content.firstElementChild.cloneNode(true);
    const typeSelect = row.querySelector('[name="level-type"]');
    const pattern = row.querySelector('[name="level-pattern"]');
    pattern.placeholder = placeholderFor(typeSelect);
    typeSelect.addEventListener("change", () => {
      pattern.placeholder = placeholderFor(typeSelect);
      refresh();
    });
    row.querySelector('[name="level-count"]').addEventListener("input", refresh);
    row.querySelector(".bulk-level-remove").addEventListener("click", () => {
      row.remove();
      refresh();
    });
    levelsEl.appendChild(row);
    refresh();
  }

  function levels() {
    return [...levelsEl.querySelectorAll(".bulk-level")].map((row) => ({
      type: row.querySelector('[name="level-type"]').value,
      count: Number(row.querySelector('[name="level-count"]').value) || 0,
      name_pattern:
        row.querySelector('[name="level-pattern"]').value.trim() || null,
    }));
  }

  // "4 × 6 × 4 → 124 locations": counts multiply per level, and every level's
  // own locations are created too, so the total is the sum of the products.
  function refresh() {
    previewEl.hidden = true;
    // Stagger the rows like the tree they will become — the "6 racks, each
    // with 5 shelves…" nesting should be readable from the dialog itself.
    [...levelsEl.children].forEach((row, index) => {
      row.style.marginLeft = `${index * 18}px`;
      const marker = row.querySelector(".bulk-depth");
      if (marker) marker.hidden = index === 0;
    });
    const counts = levels().map((level) => level.count);
    if (!counts.length || counts.some((count) => count < 1)) {
      totalEl.textContent = "";
      return;
    }
    let branch = 1;
    let total = 0;
    for (const count of counts) {
      branch *= count;
      total += branch;
    }
    const chain = counts.join(" × ");
    totalEl.textContent = `${chain} → ${total} location${total === 1 ? "" : "s"}`;
  }

  async function post(dryRun) {
    const parent = form.querySelector('[name="parent_id"]').value;
    const resp = await fetch("/api/locations/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({
        parent_id: parent ? Number(parent) : null,
        levels: levels(),
        dry_run: dryRun,
      }),
    });
    return resp;
  }

  let submitting = false;

  document
    .getElementById("bulk-preview-btn")
    .addEventListener("click", async () => {
      if (submitting) return;
      submitting = true;
      try {
        errorEl.hidden = true;
        previewEl.hidden = true;
        let resp;
        try {
          resp = await post(true);
        } catch {
          errorEl.textContent = "Could not reach the server. Please try again.";
          errorEl.hidden = false;
          return;
        }
        if (!resp.ok) {
          errorEl.textContent = await errorMessage(resp);
          errorEl.hidden = false;
          return;
        }
        const result = await resp.json();
        const sample = result.sample_paths
          .map((path) => `<li>${esc(path)}</li>`)
          .join("");
        const more = result.total - result.sample_paths.length;
        previewList.innerHTML =
          sample + (more > 0 ? `<li class="muted">… ${result.total} in total</li>` : "");
        previewEl.hidden = false;
      } finally {
        submitting = false;
      }
    });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    try {
      errorEl.hidden = true;
      let resp;
      try {
        resp = await post(false);
      } catch {
        errorEl.textContent = "Could not reach the server. Please try again.";
        errorEl.hidden = false;
        return;
      }
      if (!resp.ok) {
        errorEl.textContent = await errorMessage(resp);
        errorEl.hidden = false;
        return;
      }
      window.location.reload();
    } finally {
      submitting = false;
    }
  });

  document.getElementById("bulk-add-level").addEventListener("click", addLevel);

  const openBtn = document.getElementById("generate-locations-btn");
  if (openBtn) {
    openBtn.addEventListener("click", () => {
      form.reset();
      levelsEl.replaceChildren();
      addLevel();
      errorEl.hidden = true;
      previewEl.hidden = true;
      dialog.showModal();
    });
  }
})();

// Scan putaway on the components page: a scanned bag resolves to the component it
// holds, and then ASKS what to do with it — open its details, add stock, or move
// stock to a shelf you scan. The scanning itself lives in scan_putaway.js, which
// this adapter configures; the chooser's markup is templates/_scan_choice.html.
//
// A scan used to mean one thing, "move this stock", which is only right when the
// bag is being put away. The same bag in hand is just as often one you want to
// read about or top up, and both of those were a scan followed by hunting for the
// row. So the scan asks, and each answer also sits on a single key: at the bench
// one hand holds the scanner and the other the bag.
//
// "Move" is this page's own shape of save. A component has no location field, its
// stock does, so a move takes a count out of one shelf and puts it on another —
// and because the dialog now names the SOURCE, a part stocked in several places is
// no longer a dead end that had to be sorted out from its own page.
(() => {
  if (!document.getElementById("scan-panel")) return;

  const dialog = document.getElementById("scan-choice-dialog");
  const article = document.getElementById("scan-choice-article");
  const partEl = document.getElementById("scan-choice-part");
  const descEl = document.getElementById("scan-choice-desc");
  const noteEl = document.getElementById("scan-choice-note");
  const buttons = new Map(
    [...(dialog?.querySelectorAll("[data-choice]") ?? [])].map((b) => [
      b.dataset.choice,
      b,
    ]),
  );

  function describe(component) {
    return [component.manufacturer, component.description]
      .filter(Boolean)
      .join(" · ");
  }

  // The handlers for the bag currently being asked about, or null between scans.
  // A missing entry means that answer isn't available for this component (no
  // stock → nothing to move), which is also what greys the button out.
  let choices = null;
  // Raised when an answer is given, and lowered by the close handler that answer
  // triggers. A LATCH rather than a flag held across dialog.close(), because
  // close() does not fire `close` synchronously — the spec queues a task for it.
  // Held across that gap, the handler sees the answer whenever it actually
  // arrives, and the flow does not depend on which side of run() that lands.
  let answered = false;

  function choose(name) {
    const run = choices?.[name];
    if (!run) return; // not offered for this component
    choices = null; // one answer per scan
    answered = true;
    dialog.close();
    run();
  }

  for (const [name, button] of buttons) {
    button.addEventListener("click", () => choose(name));
  }

  const KEYS = { a: "add", d: "details", m: "move" };

  dialog?.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.key === "Enter" || event.key === " ") {
      // This dialog opens the instant a scan lands, and a wedge scanner ends its
      // payload with Enter (some payloads carry spaces too). Arriving unasked at
      // whatever holds focus, those keys would answer for the user. Focus starts
      // on the article, which has nothing to activate — so swallow them only
      // there, and leave them working for anyone who has Tabbed to a button.
      if (document.activeElement === article) event.preventDefault();
      return;
    }
    const name = KEYS[event.key.toLowerCase()];
    if (!name || !choices?.[name]) return;
    event.preventDefault();
    choose(name);
  });

  dialog?.addEventListener("close", () => {
    choices = null;
    if (answered) {
      // An answer owns what happens next, and says for itself when it is done:
      // Add and Move release the queue when their own dialog closes, and Details
      // never does — it is leaving the page, and draining a second bag into a
      // lookup (and another chooser) in the moment before it goes is exactly the
      // flicker this guard exists to prevent.
      answered = false;
      return;
    }
    // Dismissed without answering: the bag is dropped, and a scan that arrived
    // while the question was up gets its turn.
    scan.resume();
  });

  // The Add dialog is the other modal that can hold the screen after a scan; a
  // scan held back behind it is released the same way. (`scan` is assigned at the
  // bottom of this IIFE — both handlers can only fire long after that.)
  document
    .getElementById("stock-dialog")
    ?.addEventListener("close", () => scan.resume());

  // A move: which pile it comes out of, how many, and the shelf it goes to. The
  // destination is deliberately left empty — with several piles in play, a
  // prefilled "to" is just a wrong guess wearing a confident face.
  function moveTarget(component, label, held) {
    return {
      title: "Move stock",
      label,
      description: describe(component),
      locationId: null,
      quantity: null, // the chosen source sets the count and its ceiling
      maxQuantity: null,
      quantityHint: "",
      sources: held.map((l) => ({ id: l.id, path: l.path, quantity: l.quantity })),
      async save(locationId, path, quantity, fromId) {
        if (locationId === fromId) {
          // Nothing to do — but say so instead of showing a green "moved" toast
          // for a move that never happened. A count smaller than the pile makes
          // it a refusal: it states an intent this no-op cannot satisfy.
          const whole = held.find((l) => l.id === fromId)?.quantity;
          throw new ScanMiss(
            quantity === whole
              ? `Already in ${path} — nothing moved.`
              : `Already in ${path} — the ${quantity} you typed went nowhere. ` +
                "Scan the shelf it should move to.",
          );
        }
        const moved = await scanFetch("/api/stock/move", "POST", {
          component_id: component.id,
          from_location_id: fromId,
          to_location_id: locationId,
          quantity,
        });
        if (!moved.ok) throw new ScanMiss(await errorMessage(moved));
        // The table shows totals, not places, so a move leaves it accurate —
        // nothing to refresh.
      },
    };
  }

  function askWhatNext(component) {
    const label = component.mpn || `Component #${component.id}`;
    const held = component.locations;
    partEl.textContent = label;
    descEl.textContent = describe(component);
    descEl.hidden = !descEl.textContent;
    noteEl.textContent = held.length
      ? ""
      : "No stock on record yet — add some before there is anything to move.";
    choices = {
      details: () => {
        window.location = `/components/${component.id}`;
      },
      // The same dialog the row's own Add button opens, so there is one way to
      // add stock and not two that could drift apart.
      add: window.openStockDialog
        ? () =>
            window.openStockDialog("add", component.id, () => {
              // An add changes the component's total, which the table's Qty
              // column shows. Refresh that in place — loadTable() is app.js's
              // global on this page — rather than reloading, which would wipe
              // the confirmation the dialog raises.
              if (typeof loadTable === "function") return loadTable();
            })
        : null,
      move: held.length ? () => scan.fileTarget(moveTarget(component, label, held)) : null,
    };
    for (const [name, button] of buttons) button.disabled = !choices[name];
    dialog.showModal();
    article.focus(); // see the markup comment in _scan_choice.html
  }

  const stockDialog = document.getElementById("stock-dialog");

  const scan = window.initScanPutaway({
    // While one of these has the screen, a scan waits rather than resolving into
    // a second question on top of the first. The chooser is the half that does the
    // work: a bag scanned during the previous lookup is queued, and the queue
    // drains the moment that lookup lands — with the chooser already up. The Add
    // dialog is belt-and-braces, and no test pins it: nothing reaches drainQueue
    // while it is open (the answer's latch holds the queue, and the collector
    // stands down for a foreign modal, so no fresh scan is even taken). It stays
    // because `resume()` is wired to that dialog's close, and the pair should
    // agree. The New Component dialog is deliberately NOT here — that one wants
    // the next code, and swaps it into its import field.
    blocked: () => Boolean(dialog?.open || stockDialog?.open),

    async resolve(code) {
      const resp = await scanFetch("/api/components/scan", "POST", { code });
      if (!resp.ok) throw new ScanMiss(await errorMessage(resp));
      const { identifiers, matches } = await resp.json();
      const seen = identifiers.join(" / ") || "this code";
      if (!matches.length) {
        // Nothing in the inventory holds this code yet. Instead of a dead-end
        // miss, open the New Component dialog and hand it the scanned code: its
        // import field looks the part up and pre-fills the form, so a brand-new
        // bag goes straight from scan to create without rescanning. Returning
        // nothing tells scan_putaway the scan was taken care of here.
        if (window.openComponentDialog) {
          // Keep the diagnostic the miss used to carry: name what was scanned, so
          // if the user cancels (or the shop lookup can't resolve it either) the
          // scan isn't left with no trace on the now-blank panel.
          showToast(`${seen} isn't in the inventory yet — creating it.`, {
            tone: "ok",
          });
          window.openComponentDialog(
            (created) => {
              window.location = `/components/${created.id}`;
            },
            null,
            { importCode: code },
          );
          return null;
        }
        throw new ScanMiss(`No component matches ${seen}.`);
      }
      if (matches.length > 1) {
        throw new ScanMiss(
          `${matches.length} components share ${seen} — open one from the table.`,
        );
      }
      // One component, and more than one thing you might want with it. Ask, and
      // report the scan as handled: whichever answer comes back drives the rest.
      askWhatNext(matches[0]);
      return null;
    },
  });
})();

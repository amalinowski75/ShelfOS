// Label-printer setup page: keep the test address in step with the port field,
// and ask the server to try the connection.
//
// The download itself is a plain form GET with no JavaScript at all — the
// browser saves the response because of its Content-Disposition, which is one
// less moving part than building a Blob and clicking an invented link.
(function () {
  const probeField = document.getElementById("probe-device");
  const button = document.getElementById("probe-btn");
  const output = document.getElementById("probe-result");
  if (!probeField || !button || !output) return;

  // The address here is the SERVER's end of the tunnel, rendered by the server
  // and left alone: it is fixed by that machine's ssh configuration. It used to
  // follow the port field above, which is this computer's — so changing that
  // pointed the test at a port nothing was ever going to answer on.

  function show(text, state) {
    output.textContent = text;
    output.dataset.state = state;
    output.hidden = false;
  }

  button.addEventListener("click", async () => {
    const device = probeField.value.trim();
    if (!device) {
      show("This ShelfOS has no printer port to test.", "bad");
      return;
    }
    button.disabled = true;
    show("Asking the printer…", "waiting");
    try {
      const response = await fetch(
        `/api/labels/setup/probe?device=${encodeURIComponent(device)}`,
        { headers: { Accept: "application/json" } },
      );
      const body = await response.json();
      if (!response.ok) {
        show(body.detail || "The test could not be run.", "bad");
        return;
      }
      show(body.detail, body.answered && !body.errors.length ? "good" : "bad");
    } catch (error) {
      show(`Could not reach ShelfOS: ${error}`, "bad");
    } finally {
      button.disabled = false;
    }
  });
})();

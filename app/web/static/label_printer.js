// Label-printer setup page: keep the test address in step with the port field,
// and ask the server to try the connection.
//
// The download itself is a plain form GET with no JavaScript at all — the
// browser saves the response because of its Content-Disposition, which is one
// less moving part than building a Blob and clicking an invented link.
(function () {
  const portField = document.getElementById("bridge_port");
  const probeField = document.getElementById("probe-device");
  const button = document.getElementById("probe-btn");
  const output = document.getElementById("probe-result");
  if (!portField || !probeField || !button || !output) return;

  function syncAddress() {
    const port = portField.value.trim();
    probeField.value = port ? `tcp://127.0.0.1:${port}` : "";
  }
  portField.addEventListener("input", syncAddress);

  function show(text, state) {
    output.textContent = text;
    output.dataset.state = state;
    output.hidden = false;
  }

  button.addEventListener("click", async () => {
    const device = probeField.value.trim();
    if (!device) {
      show("Fill in the port first.", "bad");
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

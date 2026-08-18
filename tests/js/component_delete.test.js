import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

const SCRIPTS = ["shared.js", "component_delete.js"];

// The admin-only delete affordance on the component detail page (mirrors
// _component_delete_dialog.html).
const FIXTURE = `
  <button type="button" id="component-delete-btn">Delete</button>
  <dialog id="component-delete-dialog" data-component-id="7" data-name="RC0603">
    <form id="component-delete-form">
      <ul class="consequences"><li>250 in stock, across 1 location</li></ul>
      <p id="component-delete-error" class="error" hidden></p>
      <button type="button" id="component-delete-confirm">Delete permanently</button>
    </form>
  </dialog>`;

const del = (document) =>
  document.getElementById("component-delete-confirm").click();

const PENDING_TOAST = "shelfos:pending-toast";

describe("component_delete.js", () => {
  it("asks before deleting, and does not delete on opening the dialog", async () => {
    const { document, window, fetchMock } = loadPage(FIXTURE, SCRIPTS);
    document.getElementById("component-delete-btn").click();
    await tick();

    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("deletes through the admin API and hands the news to the next page", async () => {
    const { document, window, fetchMock } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, status: 204 }),
    });
    del(document);
    await tick();

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/admin/components/7");
    expect(options.method).toBe("DELETE");
    expect(options.headers["X-CSRF-Token"]).toBe(CSRF);
    // The page it was showing no longer exists, so this navigates away rather
    // than reloading into a 404 — and the toast has to survive that.
    const pending = JSON.parse(window.sessionStorage.getItem(PENDING_TOAST));
    expect(pending.message).toBe("Deleted RC0603.");
  });

  it("shows a refusal in the dialog and stays put", async () => {
    const { document, window, fetchMock } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          status: 403,
          json: async () => ({ detail: "Admin role required" }),
        }),
    });
    del(document);
    await tick();

    const error = document.getElementById("component-delete-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Admin role required");
    // Nothing was deleted, so nothing may announce that something was.
    expect(window.sessionStorage.getItem(PENDING_TOAST)).toBeNull();
    // And the admin can try again once the reason is dealt with.
    del(document);
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("says so when the server cannot be reached", async () => {
    const { document } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => Promise.reject(new Error("down")),
    });
    del(document);
    await tick();

    const error = document.getElementById("component-delete-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("Could not reach the server");
  });

  it("deletes once, however many times the button is pressed", async () => {
    // The navigation after a successful delete is PENDING: the dialog stays on
    // screen and clickable while the next page loads, and a second DELETE would
    // answer 404 — reporting a delete that worked as one that failed.
    const { document, fetchMock } = loadPage(FIXTURE, SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, status: 204 }),
    });
    del(document);
    del(document);
    await tick();
    del(document);
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

import { describe, it, expect } from "vitest";
import { loadPage, tick, CSRF } from "./harness.js";

const SCRIPTS = ["shared.js", "component_delete.js"];

// The admin affordances on the component detail page. Only one of the two is
// ever rendered — a live component has the dialog, a deleted one the banner.
const DELETE_FIXTURE = `
  <button type="button" id="component-delete-btn">Delete</button>
  <dialog id="component-delete-dialog" data-component-id="7" data-name="RC0603">
    <form id="component-delete-form">
      <input id="component-delete-reason" name="reason" />
      <p id="component-delete-error" class="error" hidden></p>
      <button type="button" id="component-delete-confirm">Delete</button>
    </form>
  </dialog>`;

// What the page renders when the component still holds stock: the reason, and
// nothing to click that could only earn a 422.
const BLOCKED_FIXTURE = `
  <button type="button" id="component-delete-btn">Delete</button>
  <dialog id="component-delete-dialog" data-component-id="7" data-name="RC0603">
    <form id="component-delete-form">
      <p id="component-delete-blocked">Still holds 250 in stock…</p>
      <p id="component-delete-error" class="error" hidden></p>
    </form>
  </dialog>`;

const RESTORE_FIXTURE = `
  <button type="button" id="component-restore-btn" data-component-id="7">
    Restore
  </button>
  <p id="component-restore-error" class="error" hidden></p>`;

const PENDING_TOAST = "shelfos:pending-toast";
const ok = () => Promise.resolve({ ok: true, status: 204 });

describe("component_delete.js", () => {
  it("asks before deleting, and deletes nothing on opening the dialog", async () => {
    const { document, window, fetchMock } = loadPage(DELETE_FIXTURE, SCRIPTS);
    document.getElementById("component-delete-btn").click();
    await tick();

    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends the delete, with the reason when one is given", async () => {
    const { document, window, fetchMock } = loadPage(DELETE_FIXTURE, SCRIPTS, {
      fetchImpl: ok,
    });
    document.getElementById("component-delete-reason").value =
      "created by mistake";
    document.getElementById("component-delete-confirm").click();
    await tick();

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/admin/components/7");
    expect(options.method).toBe("DELETE");
    expect(options.headers["X-CSRF-Token"]).toBe(CSRF);
    // In the body, never the query string: a reason is free text about someone
    // else's part, and a query string is logged by every hop that sees it.
    expect(JSON.parse(options.body).reason).toBe("created by mistake");
    // The page still exists — it now says "deleted" — so this reloads rather
    // than navigating away, and the toast has to survive that.
    const pending = JSON.parse(window.sessionStorage.getItem(PENDING_TOAST));
    expect(pending.message).toBe("RC0603 is no longer in use.");
  });

  it("sends no reason at all when none was typed", async () => {
    const { document, fetchMock } = loadPage(DELETE_FIXTURE, SCRIPTS, {
      fetchImpl: ok,
    });
    document.getElementById("component-delete-confirm").click();
    await tick();

    // Null, not "" — the column means "no reason given", and a blank string
    // would be a reason that says nothing.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).reason).toBeNull();
  });

  it("shows a refusal in the dialog and stays put", async () => {
    const { document, window, fetchMock } = loadPage(DELETE_FIXTURE, SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          status: 422,
          json: async () => ({ detail: "Still holds 250 in stock" }),
        }),
    });
    document.getElementById("component-delete-confirm").click();
    await tick();

    const error = document.getElementById("component-delete-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Still holds 250 in stock");
    expect(window.sessionStorage.getItem(PENDING_TOAST)).toBeNull();
    // And the admin can try again once the reason is dealt with.
    document.getElementById("component-delete-confirm").click();
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("deletes once, however many times the button is pressed", async () => {
    // The reload is a PENDING navigation: the dialog stays clickable while it
    // happens, and a second DELETE would 422 as "already deleted" — reporting a
    // delete that worked as one that failed.
    const { document, fetchMock } = loadPage(DELETE_FIXTURE, SCRIPTS, {
      fetchImpl: ok,
    });
    document.getElementById("component-delete-confirm").click();
    document.getElementById("component-delete-confirm").click();
    await tick();
    document.getElementById("component-delete-confirm").click();
    await tick();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("still opens a dialog that only explains why it cannot delete", async () => {
    const { document, window, fetchMock } = loadPage(BLOCKED_FIXTURE, SCRIPTS);
    document.getElementById("component-delete-btn").click();
    await tick();

    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("restores from the banner", async () => {
    const { document, window, fetchMock } = loadPage(RESTORE_FIXTURE, SCRIPTS, {
      fetchImpl: () => Promise.resolve({ ok: true, status: 200 }),
    });
    document.getElementById("component-restore-btn").click();
    await tick();

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/admin/components/7/restore");
    expect(options.method).toBe("POST");
    expect(options.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(
      JSON.parse(window.sessionStorage.getItem(PENDING_TOAST)).message,
    ).toBe("Back in use.");
  });

  it("keeps a refused restore on screen, with a link to what is in the way", async () => {
    // The usual reason: a replacement has taken the MPN, which is exactly what
    // deleting this one allowed to happen. That answer asks the admin to go and
    // look at a DIFFERENT component, so it must not expire after five seconds —
    // and the id the API sends alongside it should be clickable, not dropped.
    const { document, window, fetchMock } = loadPage(RESTORE_FIXTURE, SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({
            detail: "A component with MPN RC0603 already exists",
            existing_id: 12,
          }),
        }),
    });
    document.getElementById("component-restore-btn").click();
    await tick();

    const error = document.getElementById("component-restore-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("already exists");
    expect(error.querySelector("a").getAttribute("href")).toBe(
      "/components/12",
    );
    expect(window.sessionStorage.getItem(PENDING_TOAST)).toBeNull();
    // Released after a failure: the admin can act on the answer and try again.
    document.getElementById("component-restore-btn").click();
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("says a plain refusal plainly, with nothing to click", async () => {
    const { document } = loadPage(RESTORE_FIXTURE, SCRIPTS, {
      fetchImpl: () =>
        Promise.resolve({
          ok: false,
          status: 422,
          json: async () => ({ detail: "That component is not deleted." }),
        }),
    });
    document.getElementById("component-restore-btn").click();
    await tick();

    const error = document.getElementById("component-restore-error");
    expect(error.textContent).toBe("That component is not deleted.");
    expect(error.querySelector("a")).toBeNull();
  });
});

import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, passwordDialogFixture, fetchBody } from "./harness.js";

const SCRIPTS = ["shared.js", "password_dialog.js"];

function open(document) {
  document.getElementById("change-password-btn").click();
}

function fill(document, current, next) {
  const form = document.getElementById("password-form");
  form.current_password.value = current;
  form.new_password.value = next;
}

function submit(document) {
  document
    .getElementById("password-form")
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("password_dialog.js", () => {
  it("opens the dialog from the top-bar button", () => {
    const { window, document } = loadPage(passwordDialogFixture(), SCRIPTS);
    open(document);
    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
  });

  it("posts the current and new password with the CSRF token", async () => {
    const { document, fetchMock } = loadPage(passwordDialogFixture(), SCRIPTS);
    open(document);
    fill(document, "oldpassword", "newpassword");

    submit(document);
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/change-password");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(fetchMock)).toEqual({
      current_password: "oldpassword",
      new_password: "newpassword",
    });
  });

  it("closes and confirms on success", async () => {
    const { window, document } = loadPage(passwordDialogFixture(), SCRIPTS);
    window.alert = vi.fn();
    open(document);
    fill(document, "oldpassword", "newpassword");

    submit(document);
    await tick();

    expect(window.HTMLDialogElement.prototype.close).toHaveBeenCalled();
    expect(window.alert).toHaveBeenCalled();
  });

  it("shows the server message on a rejected change", async () => {
    const fetchImpl = () =>
      Promise.resolve({
        ok: false,
        json: async () => ({ detail: "current password is incorrect" }),
      });
    const { document } = loadPage(passwordDialogFixture(), SCRIPTS, { fetchImpl });
    open(document);
    fill(document, "wrong", "newpassword");

    submit(document);
    await tick();

    const error = document.getElementById("password-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("current password is incorrect");
  });

  it("shows a reach-the-server message when the request throws", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { document } = loadPage(passwordDialogFixture(), SCRIPTS, { fetchImpl });
    open(document);
    fill(document, "oldpassword", "newpassword");

    submit(document);
    await tick();

    const error = document.getElementById("password-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Could not reach the server.");
  });
});

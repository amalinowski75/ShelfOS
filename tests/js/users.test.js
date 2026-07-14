import { describe, it, expect, vi } from "vitest";
import { loadPage, tick, CSRF, usersPageFixture, fetchBody } from "./harness.js";

const SCRIPTS = ["shared.js", "users.js"];

const cell = (value, row = {}) => ({
  getValue: () => value,
  getRow: () => ({ getData: () => row }),
});

function submit(document, formId) {
  document
    .getElementById(formId)
    .dispatchEvent(
      new document.defaultView.Event("submit", { cancelable: true, bubbles: true }),
    );
}

describe("users.js — columns", () => {
  it("labels columns and renders role/status badges", () => {
    const { window } = loadPage(usersPageFixture(), SCRIPTS);
    const columns = window.userColumns();
    expect(columns.map((c) => c.field)).toEqual([
      "name",
      "role",
      "is_active",
      "actions",
    ]);
    expect(columns[0].formatter(cell("<b>a"))).toBe(
      '<span class="cell-mono">&lt;b&gt;a</span>',
    );
    expect(columns[1].formatter(cell("admin"))).toContain("b-accent");
    expect(columns[1].formatter(cell("read-only"))).toContain("b-neutral");
    expect(columns[2].formatter(cell(true))).toContain("active");
    expect(columns[2].formatter(cell(false))).toContain("disabled");
  });

  it("labels the toggle button by the account's current state", () => {
    const { window } = loadPage(usersPageFixture(), SCRIPTS);
    const actions = window.userColumns()[3];
    expect(actions.formatter(cell(null, { is_active: true }))).toContain("Disable");
    expect(actions.formatter(cell(null, { is_active: false }))).toContain("Enable");
  });

  it("routes row-action clicks to the right handler", () => {
    const { window, document } = loadPage(usersPageFixture(), SCRIPTS);
    const actions = window.userColumns()[3];
    const row = { id: 5, name: "bob", role: "user", is_active: true };
    const click = (act) =>
      actions.cellClick(
        { target: { dataset: { act } } },
        { getRow: () => ({ getData: () => row }) },
      );

    click("role");
    expect(document.getElementById("user-role-form").user_id.value).toBe("5");
    expect(document.getElementById("user-role-name").textContent).toBe("bob");

    click("password");
    expect(document.getElementById("user-password-form").user_id.value).toBe("5");
  });
});

describe("users.js — loading", () => {
  it("fetches the feed and sets the data", async () => {
    const feed = { data: [{ id: 1, name: "admin", role: "admin", is_active: true }] };
    const fetchImpl = () => Promise.resolve({ ok: true, json: async () => feed });
    const { window } = loadPage(usersPageFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.spyOn(window.Tabulator.prototype, "setData");

    await window.loadUsers();
    await tick();

    expect(setData).toHaveBeenCalledWith(feed.data);
  });

  it("clears the table and warns when the feed fails", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { window } = loadPage(usersPageFixture(), SCRIPTS, { fetchImpl });
    const setData = vi.spyOn(window.Tabulator.prototype, "setData");
    window.alert = vi.fn();

    await window.loadUsers();
    await tick();

    expect(setData).toHaveBeenCalledWith([]);
    expect(window.alert).toHaveBeenCalled();
  });
});

describe("users.js — writes", () => {
  it("creates a user via the admin API", async () => {
    const { document, fetchMock } = loadPage(usersPageFixture(), SCRIPTS);
    document.getElementById("user-new-btn").click();
    const form = document.getElementById("user-new-form");
    form.username.value = "carol";
    form.password.value = "password123";
    form.elements.role.value = "user"; // form.role is the ARIA role IDL prop

    submit(document, "user-new-form");
    await tick();

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/admin/users");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(fetchBody(fetchMock)).toEqual({
      username: "carol",
      password: "password123",
      role: "user",
    });
  });

  it("defaults a new account to the 'user' role, never admin", async () => {
    const { document, fetchMock } = loadPage(usersPageFixture(), SCRIPTS);
    document.getElementById("user-new-btn").click();
    const form = document.getElementById("user-new-form");
    form.username.value = "carol";
    form.password.value = "password123";
    // Submit WITHOUT touching the role select: a blind fill must not mint admin.
    submit(document, "user-new-form");
    await tick();

    expect(fetchBody(fetchMock).role).toBe("user");
  });

  it("changes a role via PUT", async () => {
    const { window, document, fetchMock } = loadPage(usersPageFixture(), SCRIPTS);
    window.openRoleDialog({ id: 9, name: "bob", role: "user", is_active: true });
    document.getElementById("user-role-form").elements.role.value = "admin";

    submit(document, "user-role-form");
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/users/9/role");
    expect(fetchMock.mock.calls[0][1].method).toBe("PUT");
    expect(fetchBody(fetchMock)).toEqual({ role: "admin" });
  });

  it("resets a password via PUT", async () => {
    const { window, document, fetchMock } = loadPage(usersPageFixture(), SCRIPTS);
    window.openPasswordDialog({ id: 4, name: "bob", role: "user", is_active: true });
    document.getElementById("user-password-form").password.value = "brand-new-pw";

    submit(document, "user-password-form");
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/users/4/password");
    expect(fetchMock.mock.calls[0][1].method).toBe("PUT");
    expect(fetchBody(fetchMock)).toEqual({ password: "brand-new-pw" });
  });

  it("toggles active only after confirmation, sending the negated state", async () => {
    const { window, fetchMock } = loadPage(usersPageFixture(), SCRIPTS);

    window.confirm = vi.fn(() => false);
    window.toggleActive({ id: 3, name: "bob", is_active: true });
    await tick();
    expect(fetchMock).not.toHaveBeenCalled();

    window.confirm = vi.fn(() => true);
    window.toggleActive({ id: 3, name: "bob", is_active: true });
    await tick();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/users/3/active");
    expect(fetchMock.mock.calls[0][1].method).toBe("PUT");
    expect(fetchBody(fetchMock)).toEqual({ is_active: false });
  });

  it("alerts when disabling is rejected (e.g. the last-active-admin guard)", async () => {
    const fetchImpl = () =>
      Promise.resolve({
        ok: false,
        json: async () => ({ detail: "cannot disable the last active admin" }),
      });
    const { window } = loadPage(usersPageFixture(), SCRIPTS, { fetchImpl });
    window.confirm = vi.fn(() => true);
    window.alert = vi.fn();

    window.toggleActive({ id: 1, name: "admin", is_active: true });
    await tick();

    expect(window.alert).toHaveBeenCalledWith("cannot disable the last active admin");
  });

  it("alerts a reach-the-server message when the toggle request throws", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { window } = loadPage(usersPageFixture(), SCRIPTS, { fetchImpl });
    window.confirm = vi.fn(() => true);
    window.alert = vi.fn();

    window.toggleActive({ id: 3, name: "bob", is_active: true });
    await tick();

    expect(window.alert).toHaveBeenCalledWith("Could not reach the server.");
  });

  it("surfaces a server rejection (e.g. last-admin guard) in the role dialog", async () => {
    const fetchImpl = () =>
      Promise.resolve({
        ok: false,
        json: async () => ({ detail: "cannot demote the last active admin" }),
      });
    const { window, document } = loadPage(usersPageFixture(), SCRIPTS, { fetchImpl });
    window.openRoleDialog({ id: 1, name: "admin", role: "admin", is_active: true });
    document.getElementById("user-role-form").elements.role.value = "user";

    submit(document, "user-role-form");
    await tick();

    const error = document.getElementById("user-role-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("cannot demote the last active admin");
  });

  it("shows a reach-the-server message when a create request throws", async () => {
    const fetchImpl = () => Promise.reject(new Error("network"));
    const { document } = loadPage(usersPageFixture(), SCRIPTS, { fetchImpl });
    document.getElementById("user-new-btn").click();
    const form = document.getElementById("user-new-form");
    form.username.value = "carol";
    form.password.value = "password123";

    submit(document, "user-new-form");
    await tick();

    const error = document.getElementById("user-new-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Could not reach the server.");
  });
});

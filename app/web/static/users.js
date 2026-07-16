// User-account management (admin only, spec §18). A Tabulator over the
// /web/api/users feed with per-row actions — change role, reset password,
// enable/disable — plus a create dialog. Every write goes through the admin API
// (/api/admin/users…), which requires an admin session and a CSRF token.
// `csrfToken`, `esc` and `errorMessage` come from shared.js.

const usersTable = new Tabulator("#users-table", {
  layout: "fitDataFill", // natural widths + horizontal scroll; framed by frameTable
  placeholder: "No users",
  columns: userColumns(), // static columns; only the data reloads (below)
});

async function sendUserWrite(url, method, payload) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(payload),
  });
}

function roleBadge(role) {
  const cls =
    role === "admin" ? "b-accent" : role === "read-only" ? "b-neutral" : "b-ok";
  return `<span class="badge ${cls}"><span class="dot"></span>${esc(role)}</span>`;
}

function statusBadge(isActive) {
  const cls = isActive ? "b-ok" : "b-neutral";
  const label = isActive ? "active" : "disabled";
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}

function userColumns() {
  return [
    {
      title: "Username",
      field: "name",
      formatter: (cell) => `<span class="cell-mono">${esc(cell.getValue())}</span>`,
    },
    { title: "Role", field: "role", formatter: (cell) => roleBadge(cell.getValue()) },
    {
      title: "Status",
      field: "is_active",
      formatter: (cell) => statusBadge(cell.getValue()),
    },
    {
      title: "",
      field: "actions",
      headerSort: false,
      width: 260,
      hozAlign: "right",
      formatter: (cell) => {
        const row = cell.getRow().getData();
        const toggle = row.is_active ? "Disable" : "Enable";
        return `<div class="row-actions">
           <button class="btn btn-secondary btn-sm" data-act="role">Role</button>
           <button class="btn btn-secondary btn-sm" data-act="password">Password</button>
           <button class="btn btn-ghost btn-sm" data-act="toggle">${toggle}</button>
         </div>`;
      },
      cellClick: (event, cell) => {
        const act = event.target.dataset.act;
        if (!act) return;
        const row = cell.getRow().getData();
        if (act === "role") openRoleDialog(row);
        else if (act === "password") openPasswordDialog(row);
        else if (act === "toggle") toggleActive(row);
      },
    },
  ];
}

async function loadUsers() {
  try {
    const payload = await fetch("/web/api/users").then((r) => r.json());
    await usersTable.setData(payload.data);
    frameTable(usersTable);
  } catch {
    // Clear Tabulator's "loading" state and tell the admin, rather than leaving
    // the table spinning forever on a network/parse failure.
    await usersTable.setData([]);
    alert("Could not load users — refresh to try again.");
  }
}

// Ignore a re-entrant submit while a form's write is in flight (stops a fast
// double-click sending a duplicate request); each form keeps its own flag.
function makeGuard() {
  let inFlight = false;
  return async (run) => {
    if (inFlight) return;
    inFlight = true;
    try {
      await run();
    } finally {
      inFlight = false;
    }
  };
}

function openRoleDialog(row) {
  const form = document.getElementById("user-role-form");
  form.user_id.value = row.id;
  // `form.role` resolves to the ARIA role IDL property, not the <select>; reach
  // the control through the elements collection instead.
  form.elements.role.value = row.role;
  document.getElementById("user-role-name").textContent = row.name;
  document.getElementById("user-role-error").hidden = true;
  document.getElementById("user-role-dialog").showModal();
}

function openPasswordDialog(row) {
  const form = document.getElementById("user-password-form");
  form.user_id.value = row.id;
  form.password.value = "";
  document.getElementById("user-password-name").textContent = row.name;
  document.getElementById("user-password-error").hidden = true;
  document.getElementById("user-password-dialog").showModal();
}

const guardToggle = makeGuard();
function toggleActive(row) {
  const verb = row.is_active ? "Disable" : "Enable";
  if (!confirm(`${verb} ${row.name}?`)) return;
  guardToggle(async () => {
    try {
      const resp = await sendUserWrite(
        `/api/admin/users/${row.id}/active`,
        "PUT",
        { is_active: !row.is_active },
      );
      if (resp.ok) {
        await loadUsers();
      } else {
        alert(await errorMessage(resp));
      }
    } catch {
      alert("Could not reach the server.");
    }
  });
}

// --- create ---
const newUserBtn = document.getElementById("user-new-btn");
if (newUserBtn) {
  const dialog = document.getElementById("user-new-dialog");
  const form = document.getElementById("user-new-form");
  const error = document.getElementById("user-new-error");

  newUserBtn.addEventListener("click", () => {
    form.reset();
    error.hidden = true;
    dialog.showModal();
  });

  const guardNew = makeGuard();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    guardNew(async () => {
      try {
        const resp = await sendUserWrite("/api/admin/users", "POST", {
          username: form.username.value.trim(),
          password: form.password.value,
          role: form.elements.role.value, // not form.role (ARIA role IDL prop)
        });
        if (resp.ok) {
          dialog.close();
          await loadUsers();
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      }
    });
  });
}

// --- change role ---
const guardRole = makeGuard();
document
  .getElementById("user-role-form")
  ?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const error = document.getElementById("user-role-error");
    guardRole(async () => {
      try {
        const resp = await sendUserWrite(
          `/api/admin/users/${form.user_id.value}/role`,
          "PUT",
          { role: form.elements.role.value }, // not form.role (ARIA role IDL prop)
        );
        if (resp.ok) {
          document.getElementById("user-role-dialog").close();
          await loadUsers();
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      }
    });
  });

// --- reset password ---
const guardPassword = makeGuard();
document
  .getElementById("user-password-form")
  ?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const error = document.getElementById("user-password-error");
    guardPassword(async () => {
      try {
        const resp = await sendUserWrite(
          `/api/admin/users/${form.user_id.value}/password`,
          "PUT",
          { password: form.password.value },
        );
        if (resp.ok) {
          document.getElementById("user-password-dialog").close();
          form.reset(); // don't leave the just-set password sitting in the DOM
        } else {
          error.textContent = await errorMessage(resp);
          error.hidden = false;
        }
      } catch {
        error.textContent = "Could not reach the server.";
        error.hidden = false;
      }
    });
  });

usersTable.on("tableBuilt", loadUsers);

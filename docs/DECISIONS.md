# ShelfOS — Architectural Decisions (ADR-lite)

Supplement to `ShelfOS_v1.0_specification.md`. Records decisions made before
implementation starts. Decision date: 2026-07-08.

Status legend: **CONFIRMED** = agreed with the user, **DEFAULT** = assumed
default (may be changed, does not block starting).

---

## D0. Language convention  [CONFIRMED]

All repository content is written in **English**: code, comments, documentation,
identifiers, commit messages, everything. Only the working conversation with the
user is held in Polish.

## D1. Stock level — source of truth  [CONFIRMED]

- `stock_movements` is the **source of truth** for quantities.
- `component_locations.quantity` is a **cache** (materialized value), updated in
  the **same transaction** as the movement record.
- Every stock change goes through `stock_service` — no direct editing of
  `quantity` bypassing a movement.
- Reconciliation invariant: `quantity` must always equal the sum of
  `delta_quantity` for a given (component, location) pair. A helper that verifies
  this invariant is useful for tests.

## D2. Users and authentication  [CONFIRMED]

Superseded by D11 (real auth). Historical note: v1.0 initially shipped with no
login and a fixed "system user".

## D11. Authentication and authorization  [CONFIRMED]

Decided 2026-07-08 when implementing real auth (replaces the D2 stub).

- **Mechanisms (both):** signed-cookie **sessions** for the web UI and **JWT
  bearer tokens** for the JSON API. Same `SECRET_KEY` (env var) signs both.
- **Password hashing:** bcrypt (`app.services.user_service`).
- **Roles / enforcement:** `admin` > `user` > `read-only`.
  - reads (GET/HEAD): any authenticated active user (read-only included);
  - writes (POST/PUT/DELETE): `user` or `admin` — read-only is rejected (403),
    i.e. "read-only = GET only";
  - admin-only: hard delete (§20) and user management.
- **Accounts:** admin creates accounts; **no self-registration**. A first admin
  is seeded on startup from `ADMIN_USERNAME` / `ADMIN_PASSWORD` env (defaults
  `admin`/`admin`, with a warning) so there is a bootstrap account.
- **System user:** the seeded "system" user is kept as the owner of automated
  actions (seeding, imports) and **cannot log in** (no password); humans use
  their own accounts, and stock/audit records are attributed to the logged-in
  user.

## D3. EAV parameters — inheritance  [CONFIRMED]

- Component types are hierarchical (`component_types.parent_id`).
- A type **inherits** `parameter_definitions` from all ancestors.
  Example: `mosfet` exposes its own parameters **plus** those inherited from
  `transistor`.
- Effective parameter set of a type = union of definitions along the whole path
  to the root.
- Display order: `sort_order`, preserving ancestor → descendant ordering
  (parent parameters before child parameters — to confirm in practice).

## D4. Engineering units  [CONFIRMED]

- Numeric values stored in **base units** (Ω, F, V, A…).
- Full **input parsing**: user types `10k`, `100n`, `4u7`, `2.2M`
  → converted to base unit.
- Full **display formatting** with engineering prefixes (p, n, µ, m, k, M, G).
- Logic lives in a dedicated `units` module (pure, fully testable, no I/O).

---

## D5. Monetary amounts  [DEFAULT]

- `Decimal` type (never `float`) for `unit_price`, `total_price`, `total_net`,
  `total_gross`.
- **One currency per invoice** (`invoices.currency`).
- Consistency validation: sum of `invoice_lines.total_price` against
  `total_net`/`total_gross` (rounding / tax tolerance to be defined).

## D6. Parameter data types (`data_type`)  [DEFAULT]

Closed set:

| data_type | value column   | notes                                   |
|-----------|----------------|-----------------------------------------|
| `number`  | `value_num`    | base unit + `unit`                      |
| `text`    | `value_text`   | free text                               |
| `bool`    | `value_bool`   | true/false                              |
| `enum`    | `value_text`   | list of allowed values (e.g. X7R/C0G)   |

- For `enum`, a list of allowed values is bound to the parameter definition
  (e.g. table `parameter_enum_values` or a JSON column).
- Validation: a value goes only into the column matching its `data_type`.

## D7. Enums in code  [DEFAULT]

Represented as Python `Enum` (validated at model/service level):

- `mounting_type`: SMT, THT, Panel, Wire, Other
- `container_type`: reel, bag, feeder, loose, box
- `location.type`: room, rack, shelf, partition, drawer, compartment
  (extensible: feeder, box)
- `stock_movement.reason`: purchase, correction, usage, damaged_lost
- `component.status`: active, archived, obsolete, hidden

## D8. Project layout and tooling  [DEFAULT]

- `app/` layout (not `src/`):
  - `app/models/`   — SQLModel models
  - `app/services/` — business logic (component, stock, invoice, location)
  - `app/api/`      — FastAPI endpoints (later)
  - `app/web/`      — Jinja2 / HTMX (later)
  - `app/units.py`  — unit parsing/formatting
  - `app/db.py`     — session/engine
- `tests/` — unit and integration tests
- `pyproject.toml`, Python 3.12+, latest FastAPI / SQLModel
- Tooling: pytest, pytest-cov, mypy, ruff, black (spec §25)

## D9. Audit log  [DEFAULT]

A single generic `audit_log` table:

- `id`
- `entity_type` (e.g. "component", "invoice")
- `entity_id`
- `field`
- `old_value`
- `new_value`
- `user_id`
- `timestamp`

Tracking scope per spec §19: quantity, location, invoice and parameter changes,
extended since to staged import lines, user accounts and matching rules.

Creation is not audited for ordinary records — there is no prior value, and the
bulk location generator would write hundreds of rows saying "this exists now".
Two kinds are the exception, because for them the row appearing *is* the event:
a user account, which is an access grant however it is worded, and a matching
rule, which silently changes how every later import is read. Both are counted in
tens over a system's life.

Passwords are recorded as having been set and never in any form, not even the
old hash. Who set one — its owner or an admin — is told by comparing the entry's
``user_id`` with its ``entity_id``, rather than by a second field that could
drift out of agreement with the first.

**Reading it (2026-08-18).** This is the one table that grows without bound and
is never pruned, so the reader pages by the last row's `(timestamp, id)` rather
than by an offset: the log grows at the *head*, and an offset counts from the
newest row, so an entry written between two pages pushes the boundary row into
the next one and shows the same change twice — on the page whose whole purpose
is reconstructing a sequence of events. The three ways it is read (that
newest-first walk, and the who/kind filters) are indexed for the same reason,
and since there are no migrations (D10), `init_db` creates indexes an existing
database is missing — otherwise an index added to a model would reach new
installations only, working on the developer's fresh database and not on the one
that has the rows.

## D13. Deleting a component is soft  [2026-08-18]

`DELETE /api/admin/components/{id}` marks the row (`deleted_at`, `deleted_by`,
`deleted_reason` — columns the spec's §20 already provided) instead of removing
it. `hard_delete_component` stays in the service for maintenance and tests, and
is no longer reachable over HTTP.

The reason is id reuse, and it is not theoretical. A hard delete deliberately
leaves the invoice lines and stock movements that name the component behind, as
a record of what happened. `components.id` is a plain `INTEGER PRIMARY KEY`, so
SQLite assigns the next row `max(rowid) + 1` — delete the newest component and
the next one created takes its id, and with it its purchase history, its
movements and its audit trail. Measured, not assumed: deleting component #1 and
re-adding the same MPN produced a component #1 whose detail page showed two
movements it never had and an audit entry saying it had been deleted.

Nothing is lost by keeping the row. Every lookup that could block a replacement
— `find_duplicate_component`, `find_components_by_mpn`, `list_components`, the
type counts, BOM matching — already filtered `deleted_at IS NULL`; only the
setter was missing. So the same MPN and manufacturer can be entered again
immediately, which is the case that motivated deleting at all.

"Deleted" means out of use, enforced in one place (`require_live_component`, which
every write path goes through): no edits, no parameter values, no stock movements,
and absent from every list, picker and matcher. The detail page stays reachable —
audit entries and invoice lines link to it — and says so at the top.

Stock on hand refuses the delete, the way a location holding stock does: the
parts are still in the drawer, and a catalogue entry nobody can take them out of
is worse than one that is still there. The refusal sentence is produced by the
service and shown in the dialog *before* the click, so there is one wording.

Restore is offered because the row is still there; it is refused when a live
component has taken over the MPN in the meantime — which is exactly what deleting
allowed to happen — naming the part that is in the way.

## D10. Out of scope for the first slice  [DEFAULT]

Deferred (per spec, "Future"): CSV import, invoice upload/OCR, BOM, KiCad
integration, project workflows, full auth, PostgreSQL, UI tests (Playwright).

These are now prioritized in `ROADMAP.md` ("Post-v1.0 backlog"). Order set by the
user on 2026-07-08: users/auth first, then type/parameter creation, then invoice
workflow; CSV and Alembic/PostgreSQL are low priority (migration may never
happen).

## D12. Label printing goes straight at the device  [2026-08-17]

ShelfOS renders a location label itself and writes the Brother QL raster bytes to
a device path (`SHELFOS_LABEL_DEVICE`, normally `/dev/usb/lp0`). It does not go
through CUPS, and it does not use `brother_ql`'s USB backend.

Why: a device path needs no libusb, fails with an errno that turns into a
sentence worth reading, and — the decisive part — a test can point it at a
temporary file, so the whole path including the real raster encoder runs in CI
with no printer and no mocks.

What it costs, and is accepted:

- **Linux only.** ShelfOS is a self-hosted Linux app; this is not a real cost.
- **Status readback, after all.** This decision first said the opposite — that a
  one-way write cannot know anything — and that was wrong. `/dev/usb/lp*` is
  bidirectional: ask a QL for its status and it answers with 32 bytes naming the
  tape it holds, its phase, and its error bits. So ShelfOS asks before printing
  (refusing when the printer reports trouble, or holds tape the configured one
  does not match) and again afterwards, and reports whether the printer
  confirmed. It is best-effort: a device that stays silent leaves the old
  behaviour, a job reported as *sent* rather than printed.
- **CUPS must not own the printer too**, and the conflict is not a tidy `EBUSY`.
  CUPS's `usb` backend detaches the kernel `usblp` driver whenever it touches the
  device, so the node vanishes and returns while a job is in flight — observed on
  first contact with real hardware, as repeating `usblp4: removed` / re-added
  pairs in `dmesg`. Ubuntu also creates the queue by itself when the printer is
  plugged in, so this is the default state, not an unusual one.
- **The tape's colour capability is part of the job format.** A QL-800 with
  black/red tape (DK-22251) refuses a one-colour job outright, reporting an error
  with *no error bits set*. So the tape identifier drives whether the job carries
  one raster plane or two; it is not a rendering preference.
- **One process.** Jobs are serialised on a process-wide lock, so more than one
  worker process would fall back on the printer's own `EBUSY`.

The browser-printable page (`/labels/locations`) stays regardless: it works with
any printer that has a driver, including this one through CUPS.

`brother_ql_next` is a plain dependency rather than an optional extra because its
label table is the source of truth for a tape's printable width (62 mm of tape is
732 dots, of which 696 print). A copy of that table here could drift from the
library that encodes the job, and the failure mode is noise printed on real tape.

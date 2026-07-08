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

- `User` model and roles (`admin`, `user`, `read-only`) **exist** in the schema.
- In v1.0 there is **no real login** — a fixed "system user" is used
  (seeded on database initialization).
- `user_id` in stock movements and audit log points to this user.
- Full auth (sessions/passwords/token) and role enforcement — **deferred**.

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

Tracking scope per spec §19: quantity, location, invoice and parameter changes.

## D10. Out of scope for the first slice  [DEFAULT]

Deferred (per spec, "Future"): CSV import, invoice upload/OCR, BOM, KiCad
integration, project workflows, full auth, PostgreSQL, UI tests (Playwright).

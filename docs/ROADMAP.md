# ShelfOS — Implementation Roadmap (v1.0)

High-level plan. Detailed design lives in `DECISIONS.md` and `DATA_MODEL.md`.
Every feature follows the Definition of Done (spec §26): implementation + tests +
lint + type-check.

## Phase 0 — Project skeleton
- `pyproject.toml`, dependencies, Python 3.12+.
- Tooling config: pytest, pytest-cov, mypy, ruff, black.
- `app/` package layout, `app/db.py` (engine/session), test bootstrap.

## Phase 1 — Data model
- SQLModel models for all tables in `DATA_MODEL.md`.
- Enums (D7). Seed of the "system user" (D2).
- In-memory SQLite fixture for tests.

## Phase 2 — Core services (no HTTP/UI)
- `units` module: parse/format engineering values (D4).
- `component_service`: types, parameter-definition inheritance (D3),
  EAV value validation (D6).
- `stock_service`: add/take/correct → movement + quantity cache in one
  transaction (D1); reconciliation invariant helper.
- `location_service`: hierarchy management.

## Phase 3 — Invoices
- `invoice_service`: create, add lines, link components, finalize.
- Finalization: read-only lock + generate stock movements (§16).

## Phase 4 — API (FastAPI)
- REST endpoints over the services; integration tests.
- Admin delete endpoint (§20).

## Phase 5 — Web UI
- Jinja2 + HTMX + Tabulator; Pico.css.
- Generic vs type-specific component views (§11), details view (§12),
  add/take stock dialogs (§14–15), hover row actions.

Phases 0–5 are complete and tested. The remaining work is prioritized below.

## Post-v1.0 backlog (priority order)

Priorities set by the user on 2026-07-08.

1. **Users, authentication & roles (§18).** Replace the "system user" stub (D2)
   with real login and enforcement of admin / user / read-only. **Next up.**
2. **Type & parameter creation (§13).** Convenient API + flow to create component
   types and their parameter definitions (currently only low-level endpoints).
3. **Invoices — expanded workflow (§16).** Deepen invoice handling; considered
   clearly more important than CSV import or DB migration.
4. **CSV import / export (§21).** Low priority for now.
5. **Attachments upload (§10).** Actual file upload/serving; today only metadata.
6. **Alembic migrations + PostgreSQL.** Near-last; may never be needed — revisit
   only if required.

Deferred / unscheduled: BOM & KiCad integration (§22), Playwright UI tests.

UI polish/rework is held until the feature set above is complete (user has UI
notes on hold).

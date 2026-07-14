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

1. **Users, authentication & roles (§18).** ✅ **Done** (D11). Real login replaces
   the "system user" stub: bcrypt passwords, session-cookie login for the UI +
   JWT bearer tokens for the API, roles admin / user / read-only (read-only = GET
   only), admin-managed accounts, bootstrap admin seeded on startup.
2. **Type & parameter creation (§13).** ✅ **Done** (PRs #2, #3). Convenient API +
   web flow to create component types and their parameter definitions.
3. **Invoices — expanded workflow (§16).** ✅ **Done** (PRs #4, #6, #7, #8).
   Read/edit service + API, and the web UI: list/detail with component
   navigation (§9) and the create/edit/finalize workflow.
4. **Startup / bootstrap test coverage.** ✅ **Done** (PR #12). `app/db.py` and
   `app/main.py` now covered; overall Python coverage at 99%.
5. **JavaScript test harness.** ✅ **Done** (PR #11). Vitest + jsdom covering
   `shared.js` and `invoices.js`; `app.js` coverage remains a fast-follow.
6. **CSV import / export (§21).** Low priority for now.
7. **Attachments upload (§10).** Actual file upload/serving; today only metadata.
8. **Alembic migrations + PostgreSQL.** ❌ **Not planned.** SQLite is the intended
   datastore; as long as it stays good enough, it stays. Only revisit if SQLite
   proves genuinely insufficient (scale, concurrency) — not on the roadmap
   otherwise. Until then, schema changes use the recreate flow below.
9. **User management window (UI).** ✅ **Done** (PR #28). Admin-only `/users`
   screen (Tabulator + `/web/api/users` feed) to list accounts and change role,
   reset password or enable/disable them. Self-service password change for any
   role landed alongside it (PR #29).
10. **Split CI into parallel jobs.** ✅ **Done** (PR #25). `pytest` and
    `npm test` (Vitest) now run as independent parallel `python` / `web` jobs; a
    lightweight `checks` gate (`needs: [python, web]`) preserves the existing
    required status check, so no ruleset change was needed.

Deferred / unscheduled: BOM & KiCad integration (§22), Playwright UI tests,
`app.js` JS test coverage (stock dialogs + New Type builder — needs a
`window.Tabulator` stub).

### Schema changes without migrations

With no migration tool (backlog #8 is not planned), `SQLModel.metadata.create_all`
only creates missing tables — it does **not** add columns to existing ones. After
changing a model, the running SQLite file drifts and the app fails on the missing
columns. Recreate it (destroys all local data):

```bash
rm data/shelfos.db          # delete the stale SQLite file
uvicorn app.main:app        # startup rebuilds the schema + seeds the admin
python scripts/seed_demo.py # optional: repopulate fictional demo data
```

UI polish/rework is held until the feature set above is complete (user has UI
notes on hold).

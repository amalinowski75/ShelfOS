# ShelfOS

Lightweight electronic component inventory and information management system.

ShelfOS manages an inventory of electronic components: parametric search,
purchase/invoice tracking, hierarchical storage locations, and stock movements.
It is intentionally **not** an ERP, accounting, or advanced warehouse system.

## Tech stack

- **Backend:** Python 3.12+, FastAPI, SQLModel / SQLAlchemy
- **Database:** SQLite (initial), PostgreSQL (future)
- **Frontend:** Jinja2, HTMX, vanilla JavaScript, Tabulator, Pico.css

## Documentation

- [`ShelfOS_v1.0_specification.md`](ShelfOS_v1.0_specification.md) — product/architecture spec
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — architectural decisions
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — concrete data model
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — implementation roadmap

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the quality gate (Definition of Done):

```bash
ruff check .
black --check .
mypy app
pytest --cov
```

The server-rendered web UI's browser scripts have their own test suite
([Vitest](https://vitest.dev) + jsdom). It needs Node 18+; install once with
`npm ci`, then:

```bash
npm test
```

Run the API locally (interactive docs at `/docs`):

```bash
uvicorn app.main:app --reload --port 9000
```

### Authentication

The UI and API require login (decision D11). On first startup a bootstrap admin
is seeded from the environment (defaults `admin` / `admin`):

```bash
export SHELFOS_SECRET_KEY="a-long-random-secret-at-least-32-bytes"
export SHELFOS_ADMIN_USERNAME="admin"
export SHELFOS_ADMIN_PASSWORD="change-me"
```

- **Web UI:** sign in at `/login` (session cookie).
- **API:** `POST /api/auth/token` with `{"username", "password"}` returns a JWT;
  send it as `Authorization: Bearer <token>`.
- Roles: `read-only` (GET only), `user` (read + write), `admin` (+ delete and
  user management under `/api/admin/users`).

Load fictional demo data to explore the UI (a few dozen sample components):

```bash
python scripts/seed_demo.py          # only if the database is empty
python scripts/seed_demo.py --force  # add demo data anyway
```

# ShelfOS

Lightweight electronic component inventory and information management system.

ShelfOS manages an inventory of electronic components: parametric search,
purchase/invoice tracking, hierarchical storage locations, and stock movements.
It is intentionally **not** an ERP, accounting, or advanced warehouse system.

Recent changes are in [CHANGELOG.md](CHANGELOG.md).

## Tech stack

- **Backend:** Python 3.12+, FastAPI, SQLModel / SQLAlchemy
- **Database:** SQLite (initial), PostgreSQL (future)
- **Frontend:** Jinja2, vanilla JavaScript, Tabulator, and a token-based CSS
  design system — no UI framework and no front-end library beyond the table.

## Development

```bash
./shelfos.sh devel
```

A fresh clone builds the virtualenv and installs the dependencies first; later
runs go straight to serving on `http://127.0.0.1:9000`, except after a change to
`pyproject.toml`, when it reinstalls first. An empty database is offered demo
data. The database stays in this clone at `data/shelfos.db` — run a second
instance from a second clone, and give it `--port 9001`.

`shelfos.sh` is the one entry point:

| Command | What it does |
| --- | --- |
| `devel` | run a local instance from this clone, reloading on edits |
| `deploy` | install as a system service with TLS (needs root) |
| `update` | move an installed service forward, backing it up first |
| `status` | what is installed, and whether it is healthy |
| `backup` | create or restore a backup; `deploy` also schedules one nightly |
| `password` | set an account's password, with the app stopped |

`--dry-run` works on any of them: it prints what would happen, changes nothing,
and never calls `sudo`. `./shelfos.sh <command> --help` has the flags.

Settings come from `~/.ShelfOS/.env` if it exists (`SHELFOS_ENV_FILE` names a
different file). The file is parsed, not sourced, and fills in only what is not
already set — so `PORT=8080 ./shelfos.sh devel` wins over a `PORT` line in it.

The manual equivalent, if you would rather not use the script:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 9000
```

Before opening a pull request (the Definition of Done):

```bash
ruff check . && black --check . && mypy app && pytest --cov
```

The web UI scripts have their own suite ([Vitest](https://vitest.dev) + jsdom).
It needs Node 18+; install once with `npm ci`, then:

```bash
npm test
```

Interactive API docs are at `/docs` (and `/redoc`) once it is running, signed in
as an admin — they and the `/openapi.json` they read list every endpoint and its
shapes, which is as useful to someone probing the instance as to whoever runs it.

## Running it on a server

```bash
sudo ./shelfos.sh deploy
```

It asks for a hostname and a first admin password, generates the signing secret
itself, and then does the whole of [`deploy/README.md`](deploy/README.md): a
system user, the code in `/opt/shelfos`, data in `/var/lib/shelfos`, settings in
`/etc/shelfos/env`, a systemd unit, a nightly backup timer, and Caddy holding the
certificate. Run it again and it recognises the install and stops, pointing at
`update`; `--reinstall` walks the steps once more, skipping what is already done,
which is how a half-finished install is repaired. `--dry-run` shows the plan
without touching anything.

[`deploy/README.md`](deploy/README.md) has the layout it builds, the same steps
written out for doing by hand, and the handful of things that catch people out.

## Demo data

Fictional demo data to explore the UI with (a few dozen sample components):

```bash
python scripts/seed_demo.py          # only if the database is empty
python scripts/seed_demo.py --force  # add demo data anyway
```

## Documentation

Running it:

- [`docs/authentication.md`](docs/authentication.md) — signing in, the bootstrap
  admin, password rules, the sign-in throttle, roles
- [`docs/shop-integrations.md`](docs/shop-integrations.md) — distributor API
  keys, importing a part from a URL, scanning a packaging label
- [`docs/label-printing.md`](docs/label-printing.md) — location labels, the
  Brother QL path, and a printer on another machine
- [`docs/deleting-and-audit.md`](docs/deleting-and-audit.md) — what deleting a
  component does, and the audit log
- [`deploy/README.md`](deploy/README.md) — installing it on a server: the layout
  `deploy` builds, the same steps by hand, and what catches people out

How it is built:

- [`ShelfOS_v1.0_specification.md`](ShelfOS_v1.0_specification.md) — product/architecture spec
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — architectural decisions
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — concrete data model
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — implementation roadmap
- [`docs/tme-api-v2.md`](docs/tme-api-v2.md) — TME API reference (their docs are
  behind a login), for extending the TME shop integration

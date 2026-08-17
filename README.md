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
- [`docs/tme-api-v2.md`](docs/tme-api-v2.md) — TME API reference (their docs are
  behind a login), for extending the TME shop integration

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

### Shop integrations (optional)

"Import from a shop URL or a scanned code" in the New Component dialog looks a part
up via the distributor's API. Keys live in the environment (never in the database);
each shop is independent and the feature stays disabled until its key is set.

```bash
# Mouser — a Search API key (their Order API key is a different one and is rejected)
export SHELFOS_MOUSER_API_KEY="..."

# Digi-Key — OAuth2 client credentials
export SHELFOS_DIGIKEY_CLIENT_ID="..."
export SHELFOS_DIGIKEY_CLIENT_SECRET="..."
# optional: point at the sandbox
export SHELFOS_DIGIKEY_API_BASE="https://sandbox-api.digikey.com"
# optional locale. Site/currency only affect pricing and availability, which the
# import doesn't read, so they rarely matter. Keep LANGUAGE at the default "en":
# it controls the language of the parameter NAMES, and the import maps those
# against your parameter labels — a translated "Tolerancja" wouldn't match a
# "Tolerance" label and would just be dropped. Set it to your language only if
# your own parameter labels are in that language too.
export SHELFOS_DIGIKEY_LOCALE_SITE="PL"
export SHELFOS_DIGIKEY_LOCALE_CURRENCY="PLN"

# TME (tme.eu / tme.pl) — API v2. Register an application in your tme.eu customer
# panel, then generate the private key at developers.tme.eu; the pair below is the
# 50-character token and the 20-character application secret from the app details.
export SHELFOS_TME_TOKEN="..."
export SHELFOS_TME_SECRET="..."
# optional: the country used for the catalogue lookup
export SHELFOS_TME_COUNTRY="PL"
# optional. Same caveat as Digi-Key's LANGUAGE above — this translates the parameter
# NAMES, so a non-English value only helps if your own parameter labels match it.
export SHELFOS_TME_LANGUAGE="en"
```

TME returns structured parameters, so its imports are the richest of the three;
Mouser exposes specs only inside the free-text description, which the dialog parses
best-effort. Whatever a shop returns is pre-filled for review — nothing is saved
until you confirm the dialog.

### Scanning the packaging label

The same field takes a barcode/QR scan. It is focused when the dialog opens, and a
keyboard-wedge scanner ends its payload with Enter, so scanning a label is the whole
interaction. Two shapes are understood:

- **TME's QR** embeds the product URL, so it works with any scanner and imports
  exactly like a pasted URL. So does any shop URL you paste by hand.
- **Mouser's and Digi-Key's DataMatrix** is ISO 15434 / ANSI MH10.8.2: fields carrying
  data identifiers (`1P` = manufacturer part number, `30P` = the distributor's own SKU,
  `1V` = manufacturer) separated by the group separator, `GS` / `0x1D`. The part number
  is then looked up through that shop's API as usual.

**Your scanner must keep the field separators.** Many emit `GS` as a *key press* (an
F-key) rather than a character, so it never reaches the input and the fields arrive
concatenated — at which point the field boundaries are genuinely ambiguous and ShelfOS
refuses to guess, saying so rather than importing wrong data. Either configure the
scanner to send `GS`, or configure it to send a printable separator and name it:

```bash
# a visible separator your scanner sends instead of GS
export SHELFOS_SCAN_SEPARATOR="|"
```

It must be a single character that can't occur inside a field — a letter, a digit or
one of `-._/+` is ignored, since splitting on `-` would cut `1PESQ-106-33-T-S` into
three "fields" and import confidently wrong data. An ignored setting is named in a
startup warning, so it doesn't look like the feature is simply broken.

If a shop's API can't enrich the scan (its key isn't set, or the lookup fails), the
dialog is still pre-filled with the part number and manufacturer read off the label,
and says that's all it managed.

### Location labels

Every location can be printed as a label — a QR holding `SL<id>`, which scanning
puts straight into the "Set location" flow — in two ways. `/labels/locations` is a
print-ready page for an ordinary browser print dialog (`?root=<id>` for one branch,
`?sheet=1` to flow onto A4). Alongside it, ShelfOS renders the same label onto a
Brother QL's own 300 dpi grid:

```bash
# the tape in the printer, as a brother_ql identifier
export SHELFOS_LABEL_TAPE="62"          # 62 mm continuous (the roll a QL-800 ships with)
# how long each label is, on a CONTINUOUS tape (a die-cut label's length is its die's)
export SHELFOS_LABEL_LENGTH_MM="30"
export SHELFOS_LABEL_MARGIN_MM="2"
# only if the host has no DejaVu, Liberation or Noto
export SHELFOS_LABEL_FONT="/path/to/Sans.ttf"
export SHELFOS_LABEL_FONT_BOLD="/path/to/Sans-Bold.ttf"
```

`GET /api/labels/locations/<id>/preview.png` returns exactly the bitmap the printer
would receive, and takes `?tape=` / `?length=` to try a roll without touching the
environment — so the layout can be settled by looking, rather than by feeding tape
through a printer. A setting that would fail is named in a startup warning.

To print for real, point ShelfOS at the printer's device:

```bash
export SHELFOS_LABEL_DEVICE="/dev/usb/lp0"   # empty (default) = no printer; see the udev rule below
export SHELFOS_LABEL_PRINTER_MODEL="QL-800"
export SHELFOS_LABEL_MAX_JOB="50"            # labels per job
```

The raster bytes go straight to that device, so three things about the host matter.

**The device number is not stable.** `usblp` hands out the next free index, so a
printer that was `/dev/usb/lp0` can come back as `lp4` after a few replugs. A udev
rule fixes both that and the permissions (the node is `root:lp 0660`, and the
ShelfOS user is usually in neither group):

```
# /etc/udev/rules.d/99-brother-ql.rules
SUBSYSTEM=="usbmisc", ATTRS{idVendor}=="04f9", MODE="0660", GROUP="plugdev", SYMLINK+="shelfos-label"
```

```bash
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usbmisc
export SHELFOS_LABEL_DEVICE="/dev/shelfos-label"   # stable across replugs
```

Joining the `lp` group works too (`sudo usermod -aG lp $USER`), but needs a fresh
login and leaves the unstable device number.

**A QL-800 fresh out of the box is in Editor Lite mode**, in which it enumerates as
a 2 MB USB *mass storage* device holding Brother's Windows editor — no printer
interface, so no `/dev/usb/lp*` at all. Hold the Editor Lite button on the printer
for about a second until its LED goes out; it re-enumerates as a printer
(`04f9:209b`, "QL-800 Label Printer") and the node appears.

And **CUPS must not own the same printer**. Ubuntu creates a queue automatically
the moment a USB printer appears, and CUPS's `usb` backend *detaches the kernel's
`usblp` driver* whenever it touches the device. So the conflict does not show up
as a clean "device busy": the node disappears and comes back (`usblp4: removed`
in `dmesg`, then re-added a second later) and a job in flight dies mid-stream.
Remove the queue with `sudo lpadmin -x QL-800` and print through ShelfOS, or keep
the queue and print through the browser page instead — but not both.

**Two-colour tape is not optional to get right.** With DK-22251 (62 mm black/red)
loaded, a QL-800 *refuses* a one-colour job: it reports an error with no error
bits set, which looks exactly like a broken printer. Name the tape and the job is
built with two raster planes (the red one empty, unless a label uses red):

```bash
export SHELFOS_LABEL_TAPE="62red"    # DK-22251; "62" is the plain white roll
```

One thing this cannot tell you: whether tape actually came out. A one-way write to
the device succeeds even when the printer is out of tape or its cover is open — so
ShelfOS says a job was *sent* to the printer, and the printer's own LED is what
tells you the rest.

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

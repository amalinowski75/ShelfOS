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

## Documentation

- [`ShelfOS_v1.0_specification.md`](ShelfOS_v1.0_specification.md) — product/architecture spec
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — architectural decisions
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — concrete data model
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — implementation roadmap
- [`docs/tme-api-v2.md`](docs/tme-api-v2.md) — TME API reference (their docs are
  behind a login), for extending the TME shop integration

## Development

```bash
./run.sh
```

That is the whole setup: on a fresh clone it builds the virtualenv and installs the
dependencies, then serves on <http://127.0.0.1:9000>. Later runs go straight to
serving — except after a change to `pyproject.toml`, when it reinstalls first, so a
pull that adds a dependency doesn't leave you with an app that crashes on import.

It also loads `~/.ShelfOS/.env` if you keep one (shop API keys, printer, secret key
— see below); without it the app runs on its development defaults. Settings can come
from either side: `PORT=8080 ./run.sh` and a `PORT` line in that file both work, as
do `PYTHON` (which interpreter builds the venv) and `SHELFOS_ENV_FILE` (which file
to read).

The equivalent by hand, if you would rather:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 9000
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

Interactive API docs are at `/docs` (and `/redoc`) once it is running, signed in
as an admin — they and the `/openapi.json` they read list every endpoint and its
shapes, which is as useful to someone probing the instance as to whoever runs it.

### Running it on a server

`./run.sh` is for a laptop. For a machine that stays up, `deploy/` has a systemd
unit, an environment-file template, and a Caddyfile that terminates TLS and
renews its own certificate — with `deploy/README.md` explaining the setup and the
handful of things that catch people out.

### Authentication

The UI and API require login (decision D11). On first startup a bootstrap admin
is seeded from the environment (defaults `admin` / `admin`):

```bash
export SHELFOS_SECRET_KEY="a-long-random-secret-at-least-32-bytes"
export SHELFOS_ADMIN_USERNAME="admin"
export SHELFOS_ADMIN_PASSWORD="change-me"
```

Changing a password ends every sign-in made with the old one. Access tokens are
stateless and session cookies are signed, so there is no server-side record to
delete; instead each carries a fingerprint of the password it was issued against,
which is checked on every request. So an admin resetting an account's password
signs that account out everywhere, immediately — which is the point of resetting
it. Changing your own password in the browser keeps that browser signed in, and
retires your other sessions and any API tokens; an API client that changes its own
password asks for a new token.

One consequence at upgrade time: sessions and tokens issued before this existed
carry no fingerprint and are refused, so everyone signs in once more.

The bootstrap admin is seeded **only when the database has no admin who can sign
in** — so on any install past its first run, changing `SHELFOS_ADMIN_USERNAME` or
`SHELFOS_ADMIN_PASSWORD` does nothing to the account that already exists. Change
that account's password in the app (*Change password*, top bar), or with the app
stopped:

```bash
python scripts/set_password.py admin        # prompts, without echoing
python scripts/set_password.py --list       # which accounts exist
```

With `SHELFOS_ENV=production`, ShelfOS refuses to start while any admin still has
the default password — checked against the account, not against the variable, so
setting the variable is no way to satisfy it.

Passwords set through ShelfOS — an admin creating or resetting an account, a user
changing their own — must be at least 8 characters. The bootstrap admin's comes
from the environment instead, so that rule is applied at startup: with
`SHELFOS_ENV=production` a shorter `SHELFOS_ADMIN_PASSWORD` (or the default) refuses
to start; otherwise it is a warning.

Sign-ins are throttled per client address: after 10 failed attempts within
15 minutes, further attempts from that address get a 429 (with `Retry-After`) until
the oldest failure is 15 minutes old. The password is not checked while throttled,
so a locked-out address costs no bcrypt work either. An attempt counts from the
moment it starts, and its slot is given back only if the password turns out right,
so opening many connections at once does not buy more guesses than the limit.
Counting is per address, not per account, so nobody can lock a real user out by
guessing at their name. IPv6 addresses share an allowance per /64, since a single
host is normally given a whole one and could otherwise use a fresh address per
request.

```bash
export SHELFOS_LOGIN_MAX_FAILURES="10"           # 0 turns the throttle off
export SHELFOS_LOGIN_FAILURE_WINDOW_SECONDS="900"
```

Every failed attempt is logged as `Failed login for 'name' from <address>` (and a
throttled one as `Login refused for 'name' from <address>: …`), which is the line a
fail2ban filter can match to block the source at the firewall. That shape is fixed:
control characters and quotes are stripped from the name first, so no username can
break the delimiter a filter anchors on and slip past it.

The address is what uvicorn reports: behind a reverse proxy on the same host that is
the real client only because uvicorn trusts `X-Forwarded-For` from 127.0.0.1 by
default (`--forwarded-allow-ips`); a proxy elsewhere needs that option set to its
address, or every visitor shares one allowance. A transport that reports no client
address at all (a unix socket, say) turns the throttle off and says so once at
startup, rather than putting every visitor in one bucket where ten failures from
anyone would lock out everybody.

The counters are in memory and per process, which is right for the single-worker
uvicorn ShelfOS runs under.

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

# Farnell / Newark / CPC — one element14 API key, from partner.element14.com
export SHELFOS_FARNELL_API_KEY="..."
# optional: which element14 store to ask. Same caveat as the two LANGUAGE settings
# above, and the reason the default is a UK store rather than your nearest one: the
# store also decides the language of the attribute LABELS, and a translated label
# stops matching your parameter labels and is dropped. Any of their sites works,
# e.g. pl.farnell.com, de.farnell.com, www.newark.com.
export SHELFOS_FARNELL_STORE="uk.farnell.com"
```

All but Mouser return structured parameters; Mouser exposes specs only inside the
free-text description, which the dialog parses best-effort. Farnell is the one that
also states the component's case and how it mounts, so those two fields fill
themselves. Whatever a shop returns is pre-filled for review — nothing is saved until
you confirm the dialog.

A Farnell product URL ends in the order code (`…/dp/3367839`), which is element14's
own unique key, so pasting a link never has to guess between two makers sharing a
part number.

### When the same maker arrives spelled two ways

A component is identified by its manufacturer part number **and** its manufacturer,
and the shops disagree about the latter: Farnell says `ONSEMI`, a Digi-Key invoice
says `ON Semiconductor`. Left alone, those become two components for one part.

ShelfOS does not guess. When you import a part whose number is already in stock, the
dialog says so and lists what it found — an MPN really can belong to two different
companies, so which one this is, if any, is your call. If one of them is the same
part, say so: ShelfOS opens it, and if the maker was spelled differently it remembers
that spelling. Every component is then stored under the one canonical name, which is
what the tables and filters show.

Those remembered spellings are listed on the **Match rules** page under *Manufacturer
names*, where an admin can forget one. Forgetting changes only what later imports
resolve — components already stored under a name keep it.

The invoice review asks it too. A staged line whose number is already in the
inventory carries an **Already in stock?** marker; opening it lists what shares the
number, and *This is it* files the line against that component — so finalizing does
not create a second one — while recording the invoice's spelling for next time.

On a draft invoice the two tables — lines already resolved, and lines still under
review — read the same and offer the same edits, because they differ in what they
*are*, not in what you can do with them. Both show part, location, quantity, unit
price and total, in that order. Each has an inline location picker, and each has an **Edit
line** button for the invoice's own numbers (quantity, unit price, supplier part
number). A staged row additionally has **Edit component**, which sets what it will
become at finalize; the three editable things on the page are named apart, so no
two buttons read alike.

Scan putaway asks the same question. A bag label states its maker in the `1V` field,
and a scan is only a putaway when that maker is the one the part is stored under —
otherwise the same dialog opens and asks, rather than accepting stock onto a part
that merely shares a number. A label that names no maker (most 1D barcodes) is not a
disagreement, and matches on the number alone as it always has.

### Scanning the packaging label

The same field takes a barcode/QR scan. It is focused when the dialog opens, and a
keyboard-wedge scanner ends its payload with Enter, so scanning a label is the whole
interaction. Two shapes are understood:

- **TME's QR** embeds the product URL, so it works with any scanner and imports
  exactly like a pasted URL. So does any shop URL you paste by hand.
- **Mouser's, Digi-Key's and Farnell's DataMatrix** is ISO 15434 / ANSI MH10.8.2: fields
  carrying data identifiers (`1P` = manufacturer part number, `30P` or Farnell's `3P` =
  the distributor's own order code, `1V` = manufacturer) separated by the group
  separator, `GS` / `0x1D`. The part number is then looked up through that shop's API as
  usual. Which shop printed the label is worked out from the identifiers on it —
  Digi-Key's `-ND` suffix and `…Z` fields, Farnell's `3P` — falling back to Mouser, which
  prints nothing of its own. A wrong guess costs nothing but the enrichment: the shop
  answers "no product found" and the dialog fills from the label.

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
export SHELFOS_LABEL_TAPE="62red"       # DK-22251, the black/red roll in the box
# how long each label is, on a CONTINUOUS tape (a die-cut label's length is its die's)
export SHELFOS_LABEL_LENGTH_MM="30"
export SHELFOS_LABEL_MARGIN_MM="2"
# only if the host has no DejaVu, Liberation or Noto
export SHELFOS_LABEL_FONT="/path/to/Sans.ttf"
export SHELFOS_LABEL_FONT_BOLD="/path/to/Sans-Bold.ttf"
```

Pick the roll in the app: the per-location **Print** button opens a dialog with
the tapes you stock, the bitmap that tape would produce, and — when the printer
is holding something else — the question of whether to print on what is loaded
or go and change the roll. List the rolls you own so the picker is short:

```bash
export SHELFOS_LABEL_TAPES="62red,29x90,12,17x54,62x29"
```

**The size mostly settles itself.** A connected printer is asked what tape it
holds, and the label is laid out for that: swapping a 62 mm roll for a 29 mm one
changes the labels, not the settings. `SHELFOS_LABEL_TAPE` then matters for the
one thing the printer will not say — whether the roll is the black/red kind —
and as the fallback when no printer is answering. The layout follows the tape's
proportions too: a wide label puts the QR beside the name and path, a squarer one
stacks them, and a label longer than it is wide — a 12 mm roll, a 29 × 90 mm
address label — is composed along its length and turned a quarter turn, which is
how such a roll is read anyway. The code stops growing at 25 mm, where it is
already readable across a room, and on a narrow tape the margin gives way before
the code does.

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

Labels go to the printer **one at a time**, and the dialog shows how far a run
has got with a Stop beside it. That matters for the case it was built for: a
whole cabinet started by mistake is hundreds of labels, and a job handed over in
one piece lives in the printer's buffer where nothing can reach it — the power
switch, mid-label, would be the only way out. Stopping takes effect after the
label being printed, so expect one or two more to come out.

ShelfOS talks to the printer both ways: before a job it asks what tape is loaded
and refuses when that is not the configured one ("the printer has 62 mm tape
loaded, but SHELFOS_LABEL_TAPE is '29'"), or when the printer reports an open
cover, a jam or an empty roll. After the job it waits for the printer to confirm
the print. A printer that stays silent is not treated as a failure — the job is
then reported as *sent* rather than printed.

### Deleting a component

Deleting a component (admin, from the component's own page) is a **soft** delete:
the row stays and the component stops being usable — out of every list, picker
and matcher, taking no edits and no stock — while the invoice lines, stock
movements and audit entries that name it keep meaning something.

That is not tidiness. SQLite gives a new row `max(rowid) + 1`, so removing the
newest component would hand its id, and with it its purchase history and its
movements, to whatever is created next. Keeping the row costs nothing: every
lookup that could block a replacement already ignores deleted components, so the
same MPN and manufacturer can be entered again straight away — and a deleted
component can be restored from its page unless a replacement has taken its MPN.

A component still holding stock cannot be deleted: the parts are in the drawer,
and a catalogue entry nobody can take them out of is worse than one that is still
there. Take the stock out first.

Admins get `/audit`: who changed what, newest first, in words rather than the
log's own tokens (`quantity@location:5` reads as "quantity in Lab / Rack A / D1").
It walks the log a page at a time instead of pretending a few thousand rows in a
browser table is a reading experience, and it is read-only — an audit trail that
can be edited from the app it audits is not one.

The column filters narrow the query rather than the rows on screen, which
matters precisely because of that paging: filtering what is loaded would answer
"nothing" for an entry sitting one page further back. Who and what are picked
from the accounts and kinds the log actually holds; field and change are
free text, and change matches either side of an arrow, so looking for a number
does not mean remembering which column it landed in.

Show more continues from the last row shown rather than from a count, so entries
written while the page is open cannot push a row into being shown twice. Times
are UTC, and the column says so.

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

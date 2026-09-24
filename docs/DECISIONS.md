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
- **No system user:** every action is attributed to the account that took it,
  imports included. The "system" user this decision originally kept as the owner
  of automated actions was a holdover from D2, when there was no authentication;
  it was seeded on every startup long after it had nothing left to own. The demo
  data still needs an actor — stock and audit rows take a `user_id` and nobody
  is signed in while they are generated — so it creates one of its own, named
  `demo` and unable to sign in (`app/seed.py`). A production install has no such
  account.

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

## D15. One part, several catalogue entries  [2026-09-17]

The same physical part reaches the shelves under more than one part number. Tape,
tray and loose bulk of one transistor carry different MPNs — the packaging suffix
is part of what you order — and a maker sometimes renumbers a part for reasons
that have nothing to do with the silicon inside it. ShelfOS keeps them as separate
components, and that stays: each one is a thing that can be ordered, received,
priced and counted. (An MPN that is not unique ACROSS manufacturers is a
different problem with a different answer — the maker is the tiebreaker there.)

What was missing is the other half: "how many of these do I have?" is answered by
the total across every entry that is the same part, and a BOM line pointed at one
of them alone reads as short while the drawer beside it is full.

So an **equivalence group** records that fact once, globally, rather than per BOM.
Two new tables (`component_equivalence_groups`, `component_equivalence_members`)
and **no column on `components`**: with no migrations, `create_all` adds a missing
table to a running installation on the next restart but never a missing column, so
a group reaches production while a `group_id` on the component would reach only a
database rebuilt from scratch.

Three rules make the group mean something:

- **A component belongs to at most one group**, enforced by a unique key on
  `component_id`. "Is the same part as" is transitive; there is deliberately no
  way to say A matches B, B matches C, and A does not match C.
- **A group of fewer than two members is deleted.** It says nothing, and left
  standing it would adopt the next part added to its remaining member — a group
  nobody created, with someone else's note already attached.
- **Joining two existing groups is refused**, with the other group named. Merging
  says every member of one equals every member of the other, which is a much
  larger claim than adding one variant, and not one to make on a user's behalf.

A part taken out of use keeps its membership: the delete is reversible, and
dropping the row would lose a decision the restore could not bring back. It
contributes no stock either way — a component cannot be deleted while its parts
are on the shelf. Its page becomes read-only for the group as well, refused in the
API and not offered in the UI, so the membership a restore is meant to bring back
cannot be dropped from the one page whose other write controls are already hidden.
A retired variant can still be dropped from a LIVE part's page, because that is a
decision about the live part.

Where the group is READ is deliberately narrow. The BOM report sums it for a
line someone has assigned, and the take will draw from it; nothing else does. The
assignment still names one component — that is the decision a person made — and
an UNRESOLVED line is not widened, because its MPN lookup is already a guess and
following that guess's group would make it a larger one with a stock figure
behind it that reads like fact.

**The take draws from the group too (#174).** Two rules order it. The gathering
branch is exhausted first, across every entry of the part: what was put out for
this board was put out for it whatever index it carries. Then the ordinary
shelves, EMPTIEST ENTRY FIRST — a part-used bag and a loose remnant go before a
sealed reel, which closes out the awkward leftovers instead of leaving a dozen
bins with nine parts in them. Ties break on the component id so the order cannot
drift between runs.

"Which shelf?" is therefore a question about an ENTRY, not about a line: the reel
and the bulk bag can each be stocked in several places and each gets its own
answer. A line stops at the first unanswered one rather than planning the next
entry around it — the run is blocked until it is answered, and once it is, that
bin may well cover the rest.

The snapshot keeps one row per entry drawn from, all sharing the designator
group. Two movements from two components cannot honestly be one row naming one of
them. ``requested`` splits in the order the parts were drawn, so the rows still
sum to what the line wanted, and what the shelves could not give is recorded
against the entry the line is ASSIGNED to — the one someone chose.

**Also known: the per-line figures are not allocated across lines.** ``stock``,
``missing`` and ``boards_possible`` each show the whole of what that line's parts
hold, exactly as the substitute suggestions do, so two lines built from one pool
of stock both show all of it. ``summary.buildable`` is the number people act on
and is NOT fooled by that: it divides each pool of shared stock by what every line
drawing on it needs per board. Grouping is what made the overlap easy to reach —
"R1 off the reel, R2 out of the bag" is now one pool — but the same held before
for two lines assigned to one component, and the fix covers both.

Chosen over the alternative of letting a BOM line name several components, which
was the shape first asked for. That version is less code, but the equivalence is
then local to one BOM and has to be re-entered on the next one; the fact is about
the parts, so it belongs to the parts.

**The components list reads it too (2026-09-24).** "Nothing else does" above no
longer holds: the list has a **Group qty** column right after Qty. It is the whole
group's stock, the row's own included — the figure the BOM report's Stock gives
an assigned line — and it is blank for a part in no group rather than repeating
Qty, so the grouped rows are the ones that stand out. Qty itself is untouched and
still counts the one entry: a second column instead of redefining the first,
because "what is in this entry's bins" is still the question behind Add, Take and
every label. The type filter narrows the rows, not the groups; a member of another
type still counts.

## D14. HTMX is not used  [2026-09-06]

D8 planned `app/web/` as "Jinja2 / HTMX". The Jinja2 half happened; the HTMX half
never did. Every page fetched `htmx.org` from a CDN and no template ever carried
an `hx-` attribute — no `htmx.` call, no `htmx:` listener, no `HX-` response
header anywhere in the tree. The script tag is removed.

What replaced it is not a decision so much as what the pages turned out to need:
server-rendered HTML, and small vanilla-JS modules that talk to the JSON API with
`fetch` where a page has to change without reloading. Reaching for HTMX now would
mean a second way of doing the thing those modules already do.

D8 is left as written. It records what was decided before implementation started,
which is what that file is for — and that argument is about THIS file, not about
planning documents generally. The v1.0 specification listed HTMX and preferred
Pico.css too, and was corrected rather than annotated: it carries no decision
date, the README presents it as the product/architecture spec, 74 docstrings cite
its section numbers as live references, and it has already been edited once
during implementation (#59). It describes the system, so it has to be right about
it.

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

Two things refuse the delete, and the refusal sentences are produced by the
service and shown in the dialog *before* the click, so there is one wording
either way. Stock on hand, as for a location holding stock: the parts are still
in the drawer, and a catalogue entry nobody can take them out of is worse than
one that is still there. And a line on a **draft** invoice — because finalizing
needs the component live, while restoring it is refused by the replacement that
deleting it invited, so the two rules would otherwise combine into an invoice
that can never be finalized at all. `add_line` refuses a deleted component from
the other side, so the trap has no entrance.

The reason travels in the request BODY, not the query string: it is free text one
person types about another's part, and a query string is written down by every
hop that sees it (the access log, a proxy, the browser history, the `Referer`)
with no retention policy. The audit log keeps it properly, with the actor and the
timestamp.

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

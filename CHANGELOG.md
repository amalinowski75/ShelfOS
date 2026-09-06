# Changelog

Notable changes, newest group first. Inside a group the order is whichever reads
best rather than a strict chronology — where one change builds on the last, they
run oldest first. This file starts at **#112 (2026-08-28)**; the 111 pull requests
before it are in `git log`, and the entries are grouped by the work rather than by
release — the project has no releases yet.

Each entry says what changed and, where it is not obvious, why. Numbers link to the
pull request, which carries the reasoning and the verification.

## Changing a password now ends the sessions made with the old one

Until this, it did not. Both ways in outlive the password they were issued
against — an access token is stateless and a session cookie is signed, so
neither has a server-side record to delete — which made "change your password"
no answer at all to the situation it exists for: someone else has your
credentials. A leaked token stayed good for its full 24 hours, and a stolen
cookie indefinitely.

- Each sign-in now carries a fingerprint of the password it was made with, and
  it is checked on every request. Setting a new password changes the hash,
  which changes the fingerprint, which retires every sign-in issued before it.
  The fingerprint is an HMAC keyed by the app secret, not the bcrypt hash: it
  travels in a JWT payload, which is signed but readable. (Django calls the
  same mechanism the session auth hash.) No schema change and no session store,
  which matters in a project with no migrations.
- An admin resetting an account's password signs that account out everywhere.
  A user changing their own in the browser stays signed in there — the change
  would otherwise sign them out of the request making it — and loses their
  other sessions and tokens.
- The admin route now refuses to reset **your own** password, and the Users
  table drops the action from your own row; *Change password* in the top bar is
  the way. That route asks for the current password first, which is what stops
  a bystander at an unlocked browser from taking the account over — a
  protection an admin was until now the only person unable to have, by
  resetting themselves through the admin route instead.
- **Everyone signs in once more after this deploy**: a session or token from
  before carries no fingerprint, and treating a missing one as acceptable would
  make the check optional at the caller's choosing.
- `report.json`, a BOM report dumped while working on the report code, is no
  longer tracked. It is one project's parts list; the repo need not carry it or
  keep its history.

## Sign-in hardening

A public instance is only as safe as its weakest password and the number of
guesses an attacker gets at it. Both now have a floor.

- **Sign-ins are throttled** per client address: ten failures in fifteen minutes
  and the address is refused (429, `Retry-After`) until the oldest failure has
  aged out — before the password is checked, so a locked-out address costs no
  bcrypt work either. Per address rather than per account, so guessing at a name
  cannot lock its owner out; per /64 for IPv6, where one host owns the whole
  allocation. An attempt counts from the moment it starts and is uncounted only
  on success, so concurrent connections cannot each pass a check none of them
  has recorded yet. Every failure is logged with the name and the address, in a
  shape no username can alter, which is what a fail2ban filter matches.
  `SHELFOS_LOGIN_MAX_FAILURES` and `SHELFOS_LOGIN_FAILURE_WINDOW_SECONDS` tune
  it; 0 turns it off.
- **Passwords must be at least 8 characters** — created, reset, or changed by
  their owner; the dialogs say so up front with `minlength`. The bootstrap
  admin's password comes from the environment and is judged at startup instead:
  fatal in production, a warning otherwise, so a development install still comes
  up on `admin`/`admin`.

## A BOM line has to name its part

The BOM report used to resolve a line by looking its MPN up in inventory. An MPN
is not unique across manufacturers, so that answer can be the wrong part — and it
was good enough to make a BOM read as ready to build while nobody had ever
confirmed half its lines. The report now describes stock only for a line someone
has assigned a component to, which is the groundwork for taking a whole BOM off
the shelves in one go.

- **#131** — Lines are `unresolved` until a person assigns a component to them;
  `in stock` / `short` / `out` are reserved for lines that have one, and one
  unresolved line caps *buildable boards* at 0. The statuses `not in inventory`
  and `no MPN` are gone: they said different things about the same situation, and
  the answer to all of them was the same — assign a component. **Existing BOMs
  will look worse before they look better**, which is the point.
- **#131** — *Assign the obvious ones* settles, in one request, every line whose
  MPN admits exactly one component and whose manufacturer does not contradict it,
  leaving a person only the lines that genuinely need judging. Beside it, *Show
  only unresolved* drives the Status column's own filter.

## Manufacturer names — one part, however it is spelled

An MPN does not identify a part: two companies really do print the same number on
different components, and one company is printed several ways. ShelfOS now asks
rather than guesses, and remembers the answer. All four ways a part enters the
catalog ask the same question, through one shared rule.

- **#116** — Importing a part whose number is already in stock under a different
  maker's name shows what it found and lets you say which, if any, is the same
  part. Answering also records that spelling, so the question is asked once.
- **#118** — Match rules page grows a *Manufacturer names* section: an admin can
  see every recorded spelling and forget one. Before this the recording was a
  one-way door — not even editing the component could undo it, because that write
  path canonicalises too.
- **#119** — Scan putaway stopped matching on the part number alone. A bag label
  states its maker in the `1V` field; a scan is a putaway only when that maker is
  the one the part is stored under. Silence on either side is not disagreement —
  most 1D barcodes name nobody, and a Farnell invoice prints no manufacturer
  column at all.
- **#120** — The invoice review asks it too. A staged line whose number is already
  in stock carries an *Already in stock?* marker; *This is it* files the line
  against that component instead of creating a second one.
- **#127** — The dialog's promise ("picking this also records that spelling") is
  now answered per candidate by the server, which is the only side that can: the
  test folds accents and punctuation, so a client comparing lowercased strings
  promised rules that would never be created.

## Matchers within reach

Teaching the import engine a word meant leaving whatever you were doing, going to
the admin match-rules page, and re-picking domain → type → parameter from scratch.

- **#98** — A parameter carries its own **Matchers** panel: the rules scoped to it,
  listed in two sections (a shop's word for one of the allowed values, and a shop's
  label for the parameter itself), editable and deletable in place, with a quick-add
  row that stays open so a run of aliases goes in one after another rather than one
  modal per word. The new-type builder gets the same reach — its "Create matcher"
  saves the type first, then opens the matcher scoped to the fresh parameter.

  Aliases are now written as one comma-separated list per target, so a value that
  six shop spellings map onto is one row and one field rather than six rows. One
  rule per alias is still what is stored — that is what keeps the duplicate guard,
  the ordering and the audit trail per alias — and an alias may no longer contain a
  comma, since it would be read as two.

  The create dialog moved out of the admin page into `openMatcherDialog`, so all
  three callers open the same one.

## Farnell

- **#115** — A Farnell bag is recognised by the `3P` order code on its label.
- **#114** — Farnell invoice PDFs import, with the supplier's own line numbers
  and customs-code count used as integrity checks.
- **#113** — Parts import from the element14 API by URL or part number.

## Invoice review

The draft page holds rows at two stages of the same thing. They differed in what
you could do with them, and in what they were called.

- **#121** — A staged row and a real line now offer the same edits: an inline
  location picker on both, and an *Edit line* dialog over quantity, unit price and
  supplier part number on both. The three editable things on the page are named
  apart — *Edit invoice*, *Edit component*, *Edit line* — since two of them were
  called "Edit".
- **#122** — Both tables read the same: part, location, quantity, unit price,
  total, in that order, on one shared column grid. The component type and the
  supplier's code are no longer columns; a *missing* type still shows, because it
  blocks finalizing and nothing else would say so.

## Failures that used to be silent

Three `/web/api/*` readers mapped their payload with no usable catch around it.
An HTTP failure parses as JSON perfectly well and simply has no data, so the map
threw — usually out of an async event handler with nobody to catch it.

- **#123** — A failed component list no longer makes *Add line* a dead button.
- **#124** — The components table empties and says so rather than leaving the
  previous type's rows under the new filter, and the BOM picker's catch no longer
  ends one line early.
- **#125** — Both modal dialogs offer a **Retry** button instead of advice about
  retrying. Three fixes running had got the wording wrong; a button beside the
  message cannot be.

Throughout: an empty list and an unreadable one are now said differently. "You
have no components" is a claim, and a failure must not make it.

## Elsewhere

- **#129** — HTMX is gone. It was fetched from a CDN on every page load and no
  template ever carried an `hx-` attribute; what the pages actually use is
  server-rendered HTML plus small vanilla-JS modules talking to the JSON API.
- **#128** — This file. It starts at #112 because reconstructing the 111 before it
  from commit subjects would read as authoritative without being so.
- **#126** — Nine bespoke `[hidden]` resets become one rule, with the `until-found`
  exception. Author `display` beat the UA rule, so an element could be hidden as
  far as its `hidden` property and every test were concerned, and visible on screen.
- **#117** — `run.sh` sets the project up on a fresh clone and starts it thereafter.
- **#112** — The Components header carries live statistics that follow the table's
  filters.

# Changelog

Notable changes, newest group first, and within a group in whatever order reads
best — the manufacturer-name work runs oldest first, since each step builds on the
one before. This file starts at **#112 (2026-08-28)**; the 111 pull requests before
it are in `git log`, and the entries are grouped by the work rather than by release
— the project has no releases yet.

Each entry says what changed and, where it is not obvious, why. Numbers link to the
pull request, which carries the reasoning and the verification.

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

- **#126** — Nine bespoke `[hidden]` resets become one rule, with the `until-found`
  exception. Author `display` beat the UA rule, so an element could be hidden as
  far as its `hidden` property and every test were concerned, and visible on screen.
- **#117** — `run.sh` sets the project up on a fresh clone and starts it thereafter.
- **#112** — The Components header carries live statistics that follow the table's
  filters.

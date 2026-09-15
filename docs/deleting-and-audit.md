# ShelfOS — Deleting a component, and the audit log

What a delete actually does, why it works that way (D13 in
[`DECISIONS.md`](DECISIONS.md)), and what the audit trail shows.

## Deleting a component

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

## The audit log

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

# ShelfOS — Shop integrations

Looking a part up at a distributor: the API keys each shop needs, what comes
back, how a barcode or DataMatrix scan is read, and how ShelfOS handles two
shops spelling one manufacturer two ways.

## The keys each shop needs

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

## The two files an import brings home

Beside the fields, an import downloads the part's **datasheet** and its **photo**
onto the new component, so the detail page the create opens on already shows both.
Neither is fetched from your browser: the server downloads them, which means the
shop has to be willing to serve a datacenter address.

When it isn't, ShelfOS gets the file from another shop. If the shop you imported
from has no datasheet or picture for the part, or won't let the server download
it, ShelfOS asks Digi-Key and then element14 for the same part number. This
matters most for Mouser and TME: Mouser blocks both downloads, and TME blocks its
datasheets. Digi-Key's datasheet is often the maker's own PDF, which downloads
fine. One lookup at each shop serves both files.

ShelfOS takes another shop's file only when the answer is this part: the part
number has to match, and both sides must name a maker that is not a different
company. One number can belong to two makers, even in a single shop: element14
answers a Keystone `5270` with ABB's part. A shop's longer spelling of the same
name, such as "NXP USA Inc." for "NXP Semiconductors", is accepted. A component
with no maker gets nothing from another shop, because there would be nothing to
tell its part from another company's with the same number. Each shop lookup counts
against that shop's API quota, and a shop without a key is skipped.

If no shop has the file, the two are handled differently:

- **Datasheet:** the import's own address is kept as a link, and the dialog says
  so, because losing it would matter.
- **Photo:** the photo is simply absent, with no link and no notice. The component
  page opens with an empty gallery, which is the cue to add one by hand.

### Parts that came in on an invoice

A component created when an invoice is finalized is built from the invoice line
alone. The import asked the shop about the part but kept none of what it answered,
so such a component starts with no shop link, no datasheet and no photo.

Finalize fills them in. The dialog stays open and goes through the invoice's lines
one by one ("fetching product details 3/12…"). For each line, ShelfOS asks the
invoice's shop once, by the line's own catalogue number, and adds whatever the
component is missing:

- **a shop link**: the product page the shop names, or else the page built from
  the line's number;
- **a datasheet**: downloaded as a file from the invoice's shop, or else from
  Digi-Key or element14 as above. If no shop hands one over, the invoice shop's
  own datasheet is kept as a link, the same as an import does. A link doesn't
  count as having the datasheet: a later run, or **Fill in from shop**, still
  looks for the file. That includes parts imported before this, whose datasheet
  is only a link;
- **a photo**: as above, falling back to Digi-Key and element14.

What a component already has is left alone. A component that has all three is
skipped without asking any shop, so finalizing costs one lookup at the invoice's
shop for each part that is missing something, plus any photo fallbacks. A busy
invoice can take a minute, and the shop link, datasheet and photo for one line
share the same 30-second download budget. **Skip**, the ×, or Esc stops at once.
If parts are still missing something when the run ends, the dialog says how many
and what (for example, "3 of 12 still missing something (photo: 2, datasheet:
3)") and stays open until you close it.

A finalized invoice has a **Fill in from shop** button that runs the same pass
again. Use it for invoices finalized before finalize did this, or ones whose run
was skipped.

A line with no catalogue number of its own is not looked up at the invoice's shop
at all, because the part number alone could name a different product in that
shop's catalogue. It only gets a photo, from the fallbacks, which check both the
part number and the maker. A shop that can't answer (not configured, unreachable,
or out of daily calls) isn't asked again for two minutes, so an invoice with TME
down doesn't wait out a timeout on every line.

### Photos for parts already in stock

`scripts/fetch_missing_photos.py` gives the same treatment to components that have
no photo yet. It goes through every one of them, looks the part up at the shop its
link points to, and then asks the server for a photo through the same endpoint the
import dialog uses, so the fallback to Digi-Key and element14 is the same one. It
reports what it couldn't do. It works only through the HTTP API of a running ShelfOS, so it
never opens the database:

```
python scripts/fetch_missing_photos.py --url http://localhost:9000 --user admin --dry-run
python scripts/fetch_missing_photos.py --url http://localhost:9000 --user admin
```

`--dry-run` looks each part up at its linked shop but attaches nothing. It
doesn't ask the other shops, so it can't tell whether one of them would have had
a picture. The script spaces its Mouser lookups to stay under Mouser's limit of
30 calls a minute, so a run over a few hundred parts takes several minutes.

Mouser also limits a key to about 1000 calls a day, and a run over a few hundred
parts can take two calls for each. Every lookup counts, from the import dialog,
a scanned label, an invoice and this script alike. Once the day's calls are spent,
Mouser lookups fail with "Mouser rejected the request (HTTP 403): Maximum calls
per day exceeded" until the next day. The script then stops asking Mouser for the
rest of its run. Imports from the other shops are unaffected.

## When the same maker arrives spelled two ways

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

## Scanning the packaging label

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

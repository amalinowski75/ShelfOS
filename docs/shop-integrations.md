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

The two differ in what happens when it isn't. A datasheet that can't be downloaded
is kept as a link instead, and the dialog says so — losing it would matter. A photo
that can't be downloaded is simply absent: no link, no notice. The component page
opens with an empty gallery, which is the cue to add one by hand.

Expect that of Mouser, whose images sit behind the same Akamai that already refuses
our datasheet downloads from a hosted server — the same import usually gets both
when ShelfOS runs on your own machine. TME, Digi-Key and element14 serve their
images from plain CDNs.

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

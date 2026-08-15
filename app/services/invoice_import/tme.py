"""TME (Transfer Multisort Elektronik) invoice parser.

TME lays each line out as an item row (``Lp. Article Qty SZT Price/N SZT %VAT Value``)
followed by a description line and a ``Producent: … ; Symbol producenta: … ;`` line
that may wrap across rows. The MPN is read from **Symbol producenta**, not the article
column: the article wraps mid-token (``GRM022R60J104KE1`` + ``5L``) while the symbol
field carries the whole part number. The article is still worth rebuilding — it is
TME's own ordering symbol, the one a bag's QR prints as ``PN:`` — so its wrapped tail
is glued back on from the row below (see :func:`_wrapped_article`). Numbers are Polish
(comma decimal, space thousands) and the unit price is quoted per pack
("2,01/1000 SZT").
"""

from __future__ import annotations

import re

from app.services.errors import ValidationError
from app.services.invoice_import.base import (
    ParsedInvoice,
    ParsedLine,
    dotted_date,
    to_int,
    unit_price,
)

_NUMBER = re.compile(r"Numer faktury:\s*(\S+)")
_DATE = re.compile(r"Data wystawienia faktury\*?:\s*(\d{2}\.\d{2}\.\d{4})")
_CURRENCY = re.compile(r"w walucie:\s*([A-Z]{3})")

# An item header: "1  0402WGF1004TCE  10 000 SZT 2,01/1000 SZT 23  20,10".
# The article is non-greedy so it doesn't swallow the quantity; the per-pack size
# (group "per") is empty for a "/SZT" price (per 1).
_ITEM = re.compile(
    r"^\s*(\d+)\s+(.+?)\s+([\d \xa0]+?)\s+SZT\s+([\d.,]+)\s*/\s*(\d*)\s*SZT"
    r"\s+\d+\s+[\d.,\s]+$"
)
_STOP = re.compile(r"Razem:|Legenda:|Do zapłaty:|z przeniesienia")
_MANUFACTURER = re.compile(r"Producent:\s*(.+?)\s*;")
# The article column's observed wrap point: an article this long may be truncated.
_WRAP_WIDTH = 16
# What the wrapped tail of the article column can look like on its own row: one
# bare token of the characters TME symbols are built from. Deliberately excludes
# whitespace, ";" and ":" — the marks every description row carries.
_ARTICLE_TAIL = re.compile(r"^[0-9A-Za-z][0-9A-Za-z*./_-]*$")
_MPN = re.compile(r"Symbol producenta:\s*(.+?)\s*;")


class TmeInvoiceParser:
    shop_key = "tme"
    supplier = "TME"

    def matches(self, text: str, filename: str) -> bool:
        return "Transfer Multisort Elektronik" in text or "tme.eu" in text

    def parse(self, text: str) -> ParsedInvoice:
        lines = text.splitlines()
        number = _search(_NUMBER, text, "invoice number")
        currency = _search(_CURRENCY, text, "currency")
        invoice_date = dotted_date(_search(_DATE, text, "invoice date"))

        parsed: list[ParsedLine] = []
        header: re.Match[str] | None = None
        cont: list[str] = []

        def flush() -> None:
            if header is not None:
                parsed.append(_line(header, cont))

        for line in lines:
            match = _ITEM.match(line)
            if match:
                flush()
                header, cont = match, []
            elif header is not None:
                if _STOP.search(line):
                    flush()
                    header, cont = None, []
                else:
                    cont.append(line)
        flush()

        if not parsed:
            raise ValidationError("no invoice lines found in this TME PDF")
        # Every component line carries one "Producent:" (a shipping/charge line has
        # none), so a mismatch means an item row was dropped — fail loudly rather
        # than lose a line silently. "Producent:" is the marker, not "Symbol
        # producenta:", because the latter label can itself wrap across a page break.
        components = sum(1 for line in parsed if line.kind == "component")
        expected = text.count("Producent:")
        if components != expected:
            raise ValidationError(
                f"recognised {components} of {expected} TME lines — the invoice "
                "layout wasn't fully understood"
            )
        return ParsedInvoice(
            supplier=self.supplier,
            invoice_number=number,
            invoice_date=invoice_date,
            currency=currency,
            shop_key=self.shop_key,
            lines=parsed,
        )


def _wrapped_article(article: str, cont_lines: list[str]) -> tuple[str, list[str]]:
    """Rejoin an article column that wrapped, and hand back the rest of the rows.

    TME breaks the article column by rendered width, and its tail lands ALONE on
    the row underneath — before the description starts::

        4   DS1052-                         100 SZT   25,80/10 SZT 23   258,00
            082B2NA2060
            Kabel wstążkowy ze złączami

    Position is the evidence, which is why this can rebuild a symbol the older
    rule refused to: a description row is prose or a semicolon-delimited spec,
    never a bare part-number token, so a bare token directly under a *visibly
    truncated* article is the column's own tail and nothing else. A tail is only
    read as such when the article shows the break — it reached the column width,
    or it ends on the hyphen TME broke at. Both observed wraps qualify
    (``GRM022R60J104KE1`` at 16 characters, ``DS1052-`` on its hyphen).

    Consecutive tails are consumed together: a symbol long enough to wrap twice
    would otherwise be glued back into a string that is still truncated — a
    fabrication, which is the one outcome worth avoiding (it could collide with
    an unrelated real TME symbol and send enrichment to the wrong product).
    """
    if len(article) < _WRAP_WIDTH and not article.endswith(("-", "/", ".")):
        return article, cont_lines
    index = 0
    for row in cont_lines:
        # "__" is the page-break glyph, stripped here as it is below.
        candidate = re.sub(r"_{2,}", "", row).strip()
        if not _ARTICLE_TAIL.match(candidate):
            break
        article += candidate
        index += 1
    return article, cont_lines[index:]


def _line(header: re.Match[str], cont_lines: list[str]) -> ParsedLine:
    _, article, qty_s, price_s, per_s = header.groups()
    per = int(per_s) if per_s else 1
    printed = article.strip()
    article, cont_lines = _wrapped_article(printed, cont_lines)
    rejoined = article != printed  # the column's tail was found and glued back
    joined = " ".join(line.strip() for line in cont_lines)
    # TME prints "__" glyphs at a page break; they land mid-field ("Symbol
    # producenta: __ GRM31…") so drop underscore runs before reading the fields.
    joined = re.sub(r"\s+", " ", re.sub(r"_{2,}", " ", joined))
    manufacturer_m = _MANUFACTURER.search(joined)
    mpn_m = _MPN.search(joined)
    mpn = mpn_m.group(1).strip() if mpn_m else None
    # The article column is TME's OWN symbol — the invoice's supplier index, which
    # enrichment prefers over the MPN (it is TME's canonical key).
    symbol = article or None
    # Everything before "Producent:" is the semicolon-delimited spec line, e.g.
    # "Rezystor:thick film;SMD;0402;1MΩ;…" — kept as the component's notes.
    description = joined.split("Producent:")[0].strip() or None
    # Same wrap, but from a PDF whose text extraction glued the tail onto the
    # description row instead of leaving it standing alone. Only a fragment that
    # verifies against the MPN is read that way — without the positional evidence
    # above, a looser rule could fabricate a never-printed string.
    if description and mpn and not rejoined:
        head, _, rest = description.partition(" ")
        if rest and head and mpn.endswith(head):
            description = rest
            if (symbol or "") + head == mpn:
                symbol = mpn
    # The article column wraps at ~16 characters (both observed wraps break there).
    # An article that long whose tail was NOT found may be a truncated fragment,
    # and there is no way to tell — a truncated string offered to the API could
    # collide with an unrelated real symbol. Drop it; the MPN candidate still
    # covers the lookup, and the review row keeps the accurate parsed identity.
    if symbol and symbol != mpn and not rejoined and len(symbol) >= _WRAP_WIDTH:
        symbol = None
    return ParsedLine(
        quantity=to_int(qty_s),
        unit_price=unit_price(price_s, per, decimal_sep=","),
        manufacturer=manufacturer_m.group(1).strip() if manufacturer_m else None,
        mpn=mpn,
        description=description,
        # A charge line ("Koszty wysyłki") has no manufacturer symbol and its
        # article cell is prose, not a symbol — carry no supplier index for it.
        supplier_part_number=symbol if mpn else None,
        # A line with no manufacturer symbol is a charge, not a part (TME's
        # "Koszty wysyłki" shipping line) — the orchestrator skips it.
        kind="component" if mpn else "shipping",
    )


def _search(pattern: re.Pattern[str], text: str, what: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValidationError(f"could not read the {what} from this TME PDF")
    return match.group(1)

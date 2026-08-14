"""TME (Transfer Multisort Elektronik) invoice parser.

TME lays each line out as an item row (``Lp. Article Qty SZT Price/N SZT %VAT Value``)
followed by a description line and a ``Producent: … ; Symbol producenta: … ;`` line
that may wrap across rows. The MPN is read from **Symbol producenta**, not the article
column: the article wraps mid-token (``GRM022R60J104KE1`` + ``5L``) while the symbol
field carries the whole part number. Numbers are Polish (comma decimal, space
thousands) and the unit price is quoted per pack ("2,01/1000 SZT").
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


def _line(header: re.Match[str], cont_lines: list[str]) -> ParsedLine:
    _, article, qty_s, price_s, per_s = header.groups()
    per = int(per_s) if per_s else 1
    joined = " ".join(line.strip() for line in cont_lines)
    # TME prints "__" glyphs at a page break; they land mid-field ("Symbol
    # producenta: __ GRM31…") so drop underscore runs before reading the fields.
    joined = re.sub(r"\s+", " ", re.sub(r"_{2,}", " ", joined))
    manufacturer_m = _MANUFACTURER.search(joined)
    mpn_m = _MPN.search(joined)
    mpn = mpn_m.group(1).strip() if mpn_m else None
    # The article column is TME's OWN symbol — the invoice's supplier index, which
    # enrichment prefers over the MPN (it is TME's canonical key).
    symbol = article.strip() or None
    # Everything before "Producent:" is the semicolon-delimited spec line, e.g.
    # "Rezystor:thick film;SMD;0402;1MΩ;…" — kept as the component's notes.
    description = joined.split("Producent:")[0].strip() or None
    # The wrapped tail of the article column ("GRM022R60J104KE1" + "5L") lands at the
    # front of the description; when that leading token is a suffix of the real MPN,
    # it's the wrap fragment — drop it from the notes. Re-glue it onto the symbol
    # ONLY when article + fragment equals the MPN exactly (both observed wraps do):
    # a looser rule could fabricate a never-printed string that collides with some
    # unrelated real TME symbol and poison the API lookup, which is worse than
    # keeping the truncated article (the MPN candidate still covers the lookup).
    if description and mpn:
        head, _, rest = description.partition(" ")
        if rest and head and mpn.endswith(head):
            description = rest
            if (symbol or "") + head == mpn:
                symbol = mpn
    # The article column wraps at ~16 characters (both observed wraps break there).
    # An article that long may be a truncated fragment, and unless it verified as
    # the MPN above there is no way to tell — a truncated string offered to the API
    # could collide with an unrelated real symbol. Drop it; the MPN candidate still
    # covers the lookup, and the review row keeps the accurate parsed identity.
    if symbol and symbol != mpn and len(symbol) >= _WRAP_WIDTH:
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

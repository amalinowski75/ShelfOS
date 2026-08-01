"""Digi-Key invoice parser.

The cleanest of the three: each item is a row ("Line Ordered Cancelled Shipped
PART: <dk-pn> DESC: <text> <unit> <amount>") followed by
``MFG : <Manufacturer> (VA) / <MPN>``. Manufacturer and MPN are both clean there —
the ``(VA)`` marker is optional and the MPN may itself contain a slash
("MCP2200-I/MQ"). Numbers are dot-decimal.
"""

from __future__ import annotations

import re

from app.services.errors import ValidationError
from app.services.invoice_import.base import (
    ParsedInvoice,
    ParsedLine,
    month_date,
    to_int,
    unit_price,
)

_NUMBER = re.compile(r"Nr faktury\s+(\d+)")
_DATE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}")
_CURRENCY = re.compile(r"([A-Z]{3})\s*zł")

# An item row: "1  10  0  10  PART: …-ND  DESC: …  6.12900  61.29". The three
# integers are Ordered / Cancelled / Shipped; the two trailing decimals are the
# unit price and the line amount ($-anchored so a dotted token in DESC can't steal
# them).
_ITEM = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+PART:\s*(\S+)\s+"
    r"DESC:\s*(.+?)\s+([\d.]+)\s+([\d.]+)\s*$"
)
_STOP = re.compile(r"Kwota sprzedaży|Sales Amount|Skrzynka|Box\s+ShipMethod")
# "MFG : Diodes Incorporated (VA) / AP22615AWU-7" — manufacturer up to the first
# " / ", MPN the rest (may contain a slash).
_MFG = re.compile(r"MFG\s*:\s*(.+?)\s*/\s*(.+?)\s*$")
_VA = re.compile(r"\s*\(VA\)\s*$")


class DigiKeyInvoiceParser:
    shop_key = "digikey"
    supplier = "Digi-Key"

    def matches(self, text: str, filename: str) -> bool:
        lowered = text.lower()
        return "digikey" in lowered or "digi-key" in lowered

    def parse(self, text: str) -> ParsedInvoice:
        number = _NUMBER.search(text)
        if number is None:
            raise ValidationError(
                "could not read the invoice number from this Digi-Key PDF"
            )
        currency = _CURRENCY.search(text)
        if currency is None:
            raise ValidationError("could not read the currency from this Digi-Key PDF")

        parsed: list[ParsedLine] = []
        item: re.Match[str] | None = None
        cont: list[str] = []

        def flush() -> None:
            if item is not None:
                parsed.append(_line(item, cont))

        for line in text.splitlines():
            match = _ITEM.match(line)
            if match:
                flush()
                item, cont = match, []
            elif item is not None:
                if _STOP.search(line):
                    flush()
                    item, cont = None, []
                else:
                    cont.append(line)
        flush()

        # Each item row is the only place "PART:" appears; a mismatch means the
        # item-row regex dropped a line (an unforeseen quantity/amount format) —
        # fail loudly rather than silently lose it.
        expected = text.count("PART:")
        if not expected or len(parsed) != expected:
            raise ValidationError(
                f"recognised {len(parsed)} of {expected} Digi-Key lines — the invoice "
                "layout wasn't fully understood"
            )
        return ParsedInvoice(
            supplier=self.supplier,
            invoice_number=number.group(1),
            invoice_date=month_date(_invoice_date(text)),
            currency=currency.group(1),
            shop_key=self.shop_key,
            lines=parsed,
        )


def _invoice_date(text: str) -> str:
    """The invoice date — the 2nd date on the Order/Invoice/Ship/Document row."""
    for line in text.splitlines():
        dates = _DATE.findall(line)
        if len(dates) >= 2:
            return str(dates[1])
    raise ValidationError("could not read the invoice date from this Digi-Key PDF")


def _line(item: re.Match[str], cont_lines: list[str]) -> ParsedLine:
    _, _ordered, _cancelled, shipped, part, desc, price_s, _amount = item.groups()
    manufacturer: str | None = None
    mpn: str | None = None
    for line in cont_lines:
        mfg = _MFG.search(line)
        if mfg:
            manufacturer = _VA.sub("", mfg.group(1)).strip() or None
            mpn = mfg.group(2).strip() or None
            break
    return ParsedLine(
        quantity=to_int(shipped),
        unit_price=unit_price(price_s, decimal_sep="."),
        mpn=mpn,
        manufacturer=manufacturer,
        description=desc.strip() or None,
        supplier_part_number=part,
    )

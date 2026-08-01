"""Mouser invoice parser.

Each item is a row ("Line PN Ordered Shipped Pending UnitPrice Extended") followed
by a ``Numer katalogowy u producenta: <MPN>`` line and a description line
("<Manufacturer> <title> / <Polish category>"). The manufacturer is the leading
words of that description with no delimiter to the title, so it is NOT parsed here —
the orchestrator enriches Mouser lines from the shop API (fetch_by_mpn) by MPN, which
returns the canonical manufacturer. Numbers are comma-decimal.
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

# Invoice number + date sit on the "Customer Service <no> <date> <n> of <m>" row.
_HEADER = re.compile(r"(\d{6,})\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})\s+\d+\s+of\s+\d+")
_CURRENCY = re.compile(r"Price\((\w{3})\)")

# An item row: "1  771-NX3P1108UKZ  100  100  0  0,853  85,30". The Mouser catalogue
# number is "<digits>-<mpn>"; the three integers are Ordered / Shipped / Pending.
_ITEM = re.compile(
    r"^\s*(\d+)\s+(\d{2,4}-\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.,]+)\s+([\d.,]+)\s*$"
)
_STOP = re.compile(r"Wartość towarów|Merchandise|Informacje dotyczące")
_MPN = re.compile(r"Numer katalogowy u producenta:\s*(\S+)")
_SKIP_DESC = re.compile(r"Numer katalogowy|ECCN|TARIC|HTS|COO:")


class MouserInvoiceParser:
    shop_key = "mouser"
    supplier = "Mouser"

    def matches(self, text: str, filename: str) -> bool:
        return "Mouser Part Number" in text or "mouser.com" in text

    def parse(self, text: str) -> ParsedInvoice:
        header = _HEADER.search(text)
        if header is None:
            raise ValidationError(
                "could not read the invoice number/date from this Mouser PDF"
            )
        currency = _CURRENCY.search(text)
        if currency is None:
            raise ValidationError("could not read the currency from this Mouser PDF")

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

        # Every item carries exactly one "Numer katalogowy u producenta:" line; if
        # the item-row regex dropped one (e.g. an unforeseen quantity/price format),
        # the counts diverge — fail loudly rather than silently lose a line.
        expected = text.count("Numer katalogowy u producenta:")
        if not expected or len(parsed) != expected:
            raise ValidationError(
                f"recognised {len(parsed)} of {expected} Mouser lines — the invoice "
                "layout wasn't fully understood"
            )
        return ParsedInvoice(
            supplier=self.supplier,
            invoice_number=header.group(1),
            invoice_date=month_date(header.group(2)),
            currency=currency.group(1),
            shop_key=self.shop_key,
            lines=parsed,
        )


def _line(item: re.Match[str], cont_lines: list[str]) -> ParsedLine:
    _, mouser_pn, _ordered, shipped, _pending, price_s, _ext = item.groups()
    mpn: str | None = None
    description: str | None = None
    for line in cont_lines:
        stripped = line.strip()
        mpn_match = _MPN.search(stripped)
        if mpn_match:
            mpn = mpn_match.group(1)
            continue
        is_desc = "/" in stripped and not _SKIP_DESC.search(stripped)
        if description is None and is_desc:
            # "<Manufacturer> <title> / <Polish category>" — keep the left side as
            # notes; the manufacturer is enriched from the API, not split out here.
            description = stripped.split("/", 1)[0].strip() or None
    return ParsedLine(
        quantity=to_int(shipped),
        unit_price=unit_price(price_s, decimal_sep=","),
        mpn=mpn,
        manufacturer=None,
        description=description,
        supplier_part_number=mouser_pn,
    )

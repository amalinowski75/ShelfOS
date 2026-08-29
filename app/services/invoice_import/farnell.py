"""Farnell (element14) invoice parser.

Each item is a two-or-three line block::

    1  3772760                         TC  100 1.3300 0.00 1.3300 0.00  133.00
       SI5419DU-T1-GE3 MOSFET, P-CH, 30V, 12A, POWERPAK CHIPFET
       Tariff Code: IL 85412900

The first row is ``<line no> <order code> <unit> <qty> <list> <discount> <net>
<vat rate> <amount>``; the second carries the manufacturer part number and the
description with no delimiter between them (the MPN is the first token); the third
is the customs code, which doubles as this parser's integrity marker.

Two things are its own. **The order code is the useful key** — it is element14's own
catalogue number, so an imported line enriches by `id:` against their API and can't
land on another maker's part. And **the invoice carries two currencies**: the money
is in the buyer's, while a "For VAT Information Only" block restates the goods total
in GBP, so the currency is read off "Invoice Total <CUR> <amount>" and nowhere else.

The manufacturer is not on the invoice at all — no column for it — so lines carry
none and the orchestrator gets it from the shop API.
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

_INVOICE_NO = re.compile(r"Invoice Number\s+(\S+)")
_INVOICE_DATE = re.compile(r"Invoice Date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})")
# The amount is part of the pattern on purpose. "Invoice Total" appears on every
# page, but only the last one states the money; requiring a currency AND a number
# after it skips the empty repeats, and refusing to look anywhere else keeps the
# GBP of the VAT-information block from being taken for the invoice's own currency.
_CURRENCY = re.compile(r"Invoice Total\s+([A-Z]{3})\s+[\d.,]+")

# "1  3772760  TC  100 1.3300 0.00 1.3300 0.00  133.00" — line no, order code, unit
# of measure (TC = cut tape, EA = each), quantity, list price, discount rate, NET
# price, vat rate, amount. An order code may carry a packaging suffix (3367839RL).
_ITEM = re.compile(
    r"^\s*(\d+)\s+(\d{5,}[A-Z]{0,3})\s+([A-Z]{2,3})\s+([\d,]+)"
    r"\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s*$"
)
# A row that clearly starts an item ("<line no> <order code> …") but that the strict
# pattern could not read — an unforeseen column layout. Fail loudly and point at it
# rather than fold it silently into the previous item's continuation.
_LOOSE_ITEM = re.compile(r"^\s*\d+\s+\d{5,}[A-Z]{0,3}\s+[A-Z]{2,3}\s")
# The item table ends at the totals block. "P&P Charge" rather than the "Very
# Important" beside it: pdfplumber renders that as "Very Im portant" on one page of
# the sample, so it is not a string this parser can rely on.
_STOP = re.compile(r"P&P Charge|Invoice Subtotal")
# Every component line carries one; a delivery charge carries none.
_TARIFF = "Tariff Code:"
# A delivery charge sitting among the items, with no line number and no order code:
# "EXPRESS                          0.00     39.99" (vat rate, then amount).
#
# Matched by shape rather than by naming Farnell's delivery services, since the label
# varies. The shape is narrow — four or more capitals, no digits in the label, and
# exactly two amounts closing the line — and the blast radius is small either way:
# a charge is not imported as a component, it only adds a line to the invoice's
# "charges not added as lines" note.
_CHARGE = re.compile(
    r"^\s+([A-Z][A-Z][A-Z][A-Z][A-Z &/'-]*?)\s+([\d.]+)\s+([\d.,]+)\s*$"
)


class FarnellInvoiceParser:
    shop_key = "farnell"
    supplier = "Farnell"

    def matches(self, text: str, filename: str) -> bool:
        return "Premier Farnell" in text or "farnell.com" in text

    def parse(self, text: str) -> ParsedInvoice:
        number = _search(_INVOICE_NO, text, "invoice number")
        date_text = _search(_INVOICE_DATE, text, "invoice date")
        currency = _search(_CURRENCY, text, "currency")

        parsed: list[ParsedLine] = []
        charges: list[ParsedLine] = []
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
                continue
            if _LOOSE_ITEM.match(line):
                raise ValidationError(
                    f"could not read this Farnell line: {line.strip()!r}"
                )
            if item is None:
                continue
            if _STOP.search(line):
                flush()
                item, cont = None, []
                continue
            charge = _CHARGE.match(line)
            if charge:
                charges.append(
                    ParsedLine(
                        quantity=1,
                        unit_price=unit_price(charge.group(3), decimal_sep="."),
                        description=charge.group(1).strip(),
                        kind="shipping",
                    )
                )
                continue
            cont.append(line)
        flush()

        if not parsed:
            raise ValidationError("no invoice lines found in this Farnell PDF")
        # Every component line carries exactly one customs code, so a mismatch means
        # a row was dropped — fail loudly rather than lose a line silently. The
        # delivery charge has none, hence counting components and not len(lines).
        expected = text.count(_TARIFF)
        if len(parsed) != expected:
            raise ValidationError(
                f"recognised {len(parsed)} of {expected} Farnell lines — the invoice "
                "layout wasn't fully understood"
            )
        return ParsedInvoice(
            supplier=self.supplier,
            invoice_number=number,
            # "24 AUG 2026" is the same DD-Mon-YYYY the other invoices use, spaced
            # instead of hyphenated.
            invoice_date=month_date(date_text.replace(" ", "-")),
            currency=currency,
            shop_key=self.shop_key,
            lines=parsed + charges,
        )


def _line(item: re.Match[str], cont_lines: list[str]) -> ParsedLine:
    _, order_code, _unit, quantity, _list, _discount, net, _vat, _amount = item.groups()
    mpn: str | None = None
    description: str | None = None
    for raw in cont_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith(_TARIFF):
            continue
        # The first prose line of the block is "<MPN> <description>", with nothing
        # between them but a space.
        mpn, _, rest = stripped.partition(" ")
        description = rest.strip() or None
        break
    return ParsedLine(
        # The NET price, not the list one: it is what was actually charged per unit,
        # and on a discounted line the two differ.
        quantity=to_int(quantity),
        unit_price=unit_price(net, decimal_sep="."),
        mpn=mpn or None,
        # Not on the invoice — Farnell prints no manufacturer column. The order code
        # below is the better key anyway.
        manufacturer=None,
        description=description,
        supplier_part_number=order_code,
    )


def _search(pattern: re.Pattern[str], text: str, what: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValidationError(f"could not read the {what} from this Farnell PDF")
    return match.group(1)

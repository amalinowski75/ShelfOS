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
# number is "<digits>-<mpn>"; the three quantities are Ordered / Shipped / Pending and
# can carry a comma thousands separator ("2,500" = 2500), stripped by ``to_int``.
_ITEM = re.compile(
    r"^\s*(\d+)\s+(\d{2,4}-\S+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d.,]+)\s+([\d.,]+)\s*$"
)
# A line that clearly starts an item row ("<line> <digits>-<sku> …") — used to fail
# loudly on one the strict pattern couldn't read, instead of relying on a marker
# count: an item's part-number cell doesn't always carry the "Numer katalogowy u
# producenta:" label (seen in the wild as a bare "20 / <mpn>"), so counting the
# labels under-counts even when every row parses. The unlabelled form is read by
# _UNLABELLED_MPN below, so the MPN is recovered either way.
_LOOSE_ITEM = re.compile(r"^\s*\d+\s+\d{2,4}-\S")
# The item table ends here; freight/handling/duty live in this totals block
# (Merchandise / Handling / Freight / VAT / Tariff), never as an item row — so they
# are not mis-parsed as components (they lack the "<digits>-<mpn>" SKU shape anyway).
_STOP = re.compile(r"Wartość towarów|Merchandise|Informacje dotyczące")
_MPN = re.compile(r"Numer katalogowy u producenta:\s*(\S+)")
# The same cell without its label — seen in the wild as a bare "20 / UCLAMP2271P.TNT"
# ("<customer no> / <MPN>"). The left token must be NUMERIC: a title-less description
# ("Vishay / Rezystory") also has the two-token shape, and reading its category word
# as an MPN would poison enrichment and matching — worse than no MPN (→ review).
_UNLABELLED_MPN = re.compile(r"^(\d+)\s*/\s*(\S+)$")
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
        consumed_markers = 0

        def flush() -> None:
            nonlocal consumed_markers
            if item is not None:
                line, used_marker = _line(item, cont)
                parsed.append(line)
                consumed_markers += used_marker

        for line in text.splitlines():
            match = _ITEM.match(line)
            if match:
                flush()
                item, cont = match, []
                continue
            if _LOOSE_ITEM.match(line):
                # An item-shaped row the strict pattern couldn't read (an unforeseen
                # quantity/price format) — fail loudly and point at it rather than
                # silently folding it into the previous item's continuation.
                raise ValidationError(
                    f"could not read this Mouser line: {line.strip()!r}"
                )
            if item is not None:
                if _STOP.search(line):
                    flush()
                    item, cont = None, []
                else:
                    cont.append(line)
        flush()

        if not parsed:
            raise ValidationError("no invoice lines found in this Mouser PDF")
        # Second safety layer: every labelled part-number marker in the text must have
        # been consumed as THE marker of exactly one item. A row that stopped looking
        # item-shaped altogether (so _LOOSE_ITEM can't flag it) drops silently, but
        # its marker line remains in the text and this count catches it. Compared
        # against consumed markers, NOT len(parsed) — an item may legitimately carry
        # its part number in the unlabelled form and consume no marker.
        total_markers = text.count("Numer katalogowy u producenta:")
        if consumed_markers != total_markers:
            raise ValidationError(
                f"recognised {consumed_markers} of {total_markers} Mouser part-number "
                "cells — the invoice layout wasn't fully understood"
            )
        return ParsedInvoice(
            supplier=self.supplier,
            invoice_number=header.group(1),
            invoice_date=month_date(header.group(2)),
            currency=currency.group(1),
            shop_key=self.shop_key,
            lines=parsed,
        )


def _line(item: re.Match[str], cont_lines: list[str]) -> tuple[ParsedLine, int]:
    """One parsed item plus how many labelled part-number markers it consumed (0/1)."""
    _, mouser_pn, _ordered, shipped, _pending, price_s, _ext = item.groups()
    mpn: str | None = None
    markers = 0
    description: str | None = None
    for line in cont_lines:
        stripped = line.strip()
        mpn_match = _MPN.search(stripped)
        if mpn_match:
            if markers:
                # Two labelled markers inside one item's continuation block mean a row
                # between them stopped looking item-shaped and was swallowed — the
                # quantity/price here and this MPN would belong to DIFFERENT parts.
                # Never overwrite; fail loudly.
                raise ValidationError(
                    "two part-number cells inside one Mouser item — a row was "
                    f"not recognised (near {stripped!r})"
                )
            mpn = mpn_match.group(1)
            markers = 1
            continue
        if mpn is None and description is None:
            # The part-number cell in its unlabelled form, sitting where the labelled
            # marker normally does (before the description). Take the MPN — and don't
            # fall through, or the description branch would keep its left half ("20").
            unlabelled = _UNLABELLED_MPN.match(stripped)
            if unlabelled:
                mpn = unlabelled.group(2)
                continue
        is_desc = "/" in stripped and not _SKIP_DESC.search(stripped)
        if description is None and is_desc:
            # "<Manufacturer> <title> / <Polish category>" — keep the left side as
            # notes; the manufacturer is enriched from the API, not split out here.
            description = stripped.split("/", 1)[0].strip() or None
    parsed_line = ParsedLine(
        quantity=to_int(shipped),
        unit_price=unit_price(price_s, decimal_sep=","),
        mpn=mpn,
        manufacturer=None,
        description=description,
        supplier_part_number=mouser_pn,
    )
    return parsed_line, markers

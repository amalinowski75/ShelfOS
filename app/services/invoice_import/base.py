"""Shared types and helpers for the invoice-PDF parsers.

Each shop is a small module implementing :class:`InvoiceParser`; the registry in
``__init__`` picks one by a content signature and turns the extracted PDF text into
a :class:`ParsedInvoice`. The orchestrator (``invoice_import_service``) then matches
each :class:`ParsedLine` to a component and builds a draft invoice — the parsers here
know nothing about the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from app.services.errors import ValidationError

# Money is stored at six decimal places (decision D5); a per-N unit price
# (TME "2,01/1000 SZT" → 0.002010) is quantized to that so it lands exactly.
_MONEY_QUANTUM = Decimal("0.000001")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class ParsedLine:
    """One invoice line, as read off the PDF (pre-matching)."""

    quantity: int
    unit_price: Decimal
    mpn: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    # The shop's own catalogue number for the part (Mouser "771-…", Digi-Key
    # "…-ND"), kept as the invoice line's supplier_part_number.
    supplier_part_number: str | None = None
    # "shipping" for a freight/handling line (TME "Koszty wysyłki"): it is a real
    # invoice charge but not a component, so the orchestrator skips it.
    kind: str = "component"


@dataclass
class ParsedInvoice:
    """A whole invoice: header fields plus its lines."""

    supplier: str
    invoice_number: str
    invoice_date: date
    currency: str
    shop_key: str
    lines: list[ParsedLine] = field(default_factory=list)


@runtime_checkable
class InvoiceParser(Protocol):
    shop_key: str
    supplier: str

    def matches(self, text: str, filename: str) -> bool: ...

    def parse(self, text: str) -> ParsedInvoice: ...


def to_decimal(text: str, *, decimal_sep: str = ",") -> Decimal:
    """Parse a shop's number string to :class:`Decimal`.

    Handles the two formats these invoices use: comma-decimal with space (or NBSP)
    thousands (TME/Mouser "2 500,50", "0,853") and dot-decimal (Digi-Key "6.12900").
    The grouping separator — whichever is not the decimal one — is stripped.
    """
    cleaned = text.strip().replace("\xa0", " ")
    if decimal_sep == ",":
        cleaned = cleaned.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(" ", "").replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValidationError(f"could not read the amount {text!r}") from None


def unit_price(total_price: str, per: int = 1, *, decimal_sep: str = ",") -> Decimal:
    """A per-unit price at money precision.

    TME quotes a price per pack ("2,01/1000 SZT" → per=1000); dividing and quantizing
    to six places keeps the DB's Decimal exact instead of shipping a repeating value.
    """
    value = to_decimal(total_price, decimal_sep=decimal_sep)
    if per > 1:
        value = value / per
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def to_int(text: str) -> int:
    """Parse a quantity that may carry space/NBSP thousands ("10 000" → 10000)."""
    cleaned = text.strip().replace("\xa0", "").replace(" ", "")
    try:
        return int(cleaned)
    except ValueError:
        raise ValidationError(f"could not read the quantity {text!r}") from None


def month_date(text: str) -> date:
    """Parse a ``DD-Mon-YY`` / ``DD-Mon-YYYY`` date (Mouser, Digi-Key)."""
    parts = text.strip().split("-")
    if len(parts) != 3 or parts[1][:3].lower() not in _MONTHS:
        raise ValidationError(f"could not read the date {text!r}")
    day = int(parts[0])
    month = _MONTHS[parts[1][:3].lower()]
    year = int(parts[2])
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        raise ValidationError(f"could not read the date {text!r}") from None


def dotted_date(text: str) -> date:
    """Parse a ``DD.MM.YYYY`` date (TME "20.04.2026")."""
    parts = text.strip().split(".")
    if len(parts) != 3:
        raise ValidationError(f"could not read the date {text!r}")
    try:
        return date(int(parts[2]), int(parts[1]), int(parts[0]))
    except ValueError:
        raise ValidationError(f"could not read the date {text!r}") from None

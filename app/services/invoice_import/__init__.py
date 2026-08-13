"""Invoice-PDF parser registry.

Adding a shop = a new module implementing :class:`InvoiceParser` + one entry in
``_PARSERS``. ``parse_invoice`` is the single entry point: extract the PDF text, pick
the first parser whose content signature matches, and return a :class:`ParsedInvoice`.
The database-facing work (matching lines to components, building the draft) lives in
``app.services.invoice_import_service``.
"""

from __future__ import annotations

from app.services.errors import ValidationError
from app.services.invoice_import.base import (
    InvoiceParser,
    ParsedInvoice,
    ParsedLine,
)
from app.services.invoice_import.digikey import DigiKeyInvoiceParser
from app.services.invoice_import.mouser import MouserInvoiceParser
from app.services.invoice_import.pdf import extract_text
from app.services.invoice_import.tme import TmeInvoiceParser

_PARSERS: list[InvoiceParser] = [
    TmeInvoiceParser(),
    MouserInvoiceParser(),
    DigiKeyInvoiceParser(),
]


def parse_invoice(data: bytes, filename: str) -> ParsedInvoice:
    """Parse an uploaded invoice PDF, or raise ``ValidationError``.

    Raises if the PDF has no text (a scan) or if no parser recognises it (not a TME,
    Mouser or Digi-Key invoice).
    """
    text = extract_text(data)
    for parser in _PARSERS:
        if parser.matches(text, filename):
            return parser.parse(text)
    raise ValidationError(
        "unrecognised invoice — ShelfOS reads TME, Mouser and Digi-Key PDFs"
    )


__all__ = [
    "InvoiceParser",
    "ParsedInvoice",
    "ParsedLine",
    "parse_invoice",
]

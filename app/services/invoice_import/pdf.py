"""PDF → text extraction for invoice import.

Isolated behind one function so the parsers stay pure-text and the pdfplumber
dependency lives in a single place. ``layout=True`` keeps the page's column
geometry, which every parser here relies on (the amounts sit in fixed columns).
"""

from __future__ import annotations

import io

import pdfplumber

from app.services.errors import ValidationError


def extract_text(data: bytes) -> str:
    """Return the invoice's text, or raise ``ValidationError`` if there is none.

    A scanned (image-only) PDF yields nothing extractable — say so plainly rather
    than handing an empty string to a parser that would then "not recognise" it.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text(layout=True) or "" for page in pdf.pages]
    except Exception:
        raise ValidationError(
            "could not read this PDF — it may be corrupt or not a PDF"
        ) from None
    text = "\n".join(pages)
    if not text.strip():
        raise ValidationError(
            "this PDF has no extractable text — a scanned/image invoice can't be "
            "imported, only a text PDF the shop generated"
        )
    return text

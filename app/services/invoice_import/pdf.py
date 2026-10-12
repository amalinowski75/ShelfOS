"""PDF → text extraction for invoice import.

Isolated behind one module so the parsers stay pure-text and the pdfplumber
dependency lives in a single place. ``layout=True`` keeps the page's column
geometry, which every parser here relies on (the amounts sit in fixed columns).

Beside the text, the rows the page set wholly in a bold face — the one piece of
typography a parser has needed: TME prints its own symbol in bold, and when the
article column wraps, its tail on the row below is bold too, while the
description under it is not. That tells a wrapped symbol from a description
where the text alone could only guess.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pdfplumber

from app.services.errors import ValidationError

# Words whose tops lie this close (in points) sit on one printed row.
_ROW_TOLERANCE = 1.5


@dataclass(frozen=True)
class PdfText:
    """An invoice's text, and the rows of it set wholly in a bold face.

    ``bold_rows`` are in reading order, each whitespace-normalised (words joined
    by one space) — compare a text row with ``" ".join(row.split())``.
    """

    text: str
    bold_rows: tuple[str, ...] = ()


def _bold_rows(page: Any) -> list[str]:
    words = sorted(
        page.extract_words(extra_attrs=["fontname"]),
        key=lambda w: (w["top"], w["x0"]),
    )
    rows: list[list[dict[str, Any]]] = []
    for word in words:
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= _ROW_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [
        " ".join(w["text"] for w in sorted(row, key=lambda w: w["x0"]))
        for row in rows
        if all("bold" in str(w.get("fontname", "")).lower() for w in row)
    ]


def read_pdf(data: bytes) -> PdfText:
    """Return the invoice's text and bold rows, or raise ``ValidationError``.

    A scanned (image-only) PDF yields nothing extractable — say so plainly rather
    than handing an empty string to a parser that would then "not recognise" it.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text(layout=True) or "" for page in pdf.pages]
            bold = [row for page in pdf.pages for row in _bold_rows(page)]
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
    return PdfText(text=text, bold_rows=tuple(bold))


def extract_text(data: bytes) -> str:
    """Return the invoice's text, or raise ``ValidationError`` if there is none."""
    return read_pdf(data).text

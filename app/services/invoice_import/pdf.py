"""PDF → text extraction for invoice import.

Isolated behind one module so the parsers stay pure-text and the pdfplumber
dependency lives in a single place. ``layout=True`` keeps the page's column
geometry, which every parser here relies on (the amounts sit in fixed columns).

Beside the text, which of its lines the page set wholly in a bold face — the one
piece of typography a parser has needed: TME prints its own symbol in bold, and
when the article column wraps, its tail on the row below is bold too, while the
description under it is not. That tells a wrapped symbol from a description
where the text alone could only guess.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

import pdfplumber

from app.services.errors import ValidationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdfText:
    """An invoice's text, and which of its lines are set wholly in a bold face.

    ``bold_lines`` is aligned with ``text.split("\\n")``: one flag per line, by
    position. Empty when the typeface could not be read — a parser then works
    from the text alone.
    """

    text: str
    bold_lines: tuple[bool, ...] = ()


def _page(page: Any) -> tuple[str, list[bool]]:
    """One page's layout text and its per-line bold flags.

    Both come from the same character map pdfplumber builds the text from, so a
    flag can only ever belong to the line it was computed for — no second pass
    over the words that could cluster the rows differently.
    """
    textmap = page.get_textmap(layout=True)
    text: str = textmap.as_string
    try:
        flags: list[bool] = []
        fonts: list[str] = []
        for char, obj in [*textmap.tuples, ("\n", None)]:
            if char == "\n":
                flags.append(bool(fonts) and all("bold" in f for f in fonts))
                fonts = []
            elif obj is not None and not char.isspace():
                fonts.append(str(obj.get("fontname", "")).lower())
    except Exception:
        logger.warning("could not read the typeface of an invoice page", exc_info=True)
        flags = []
    # The map holds the text in reading order only for left-to-right, top-to-bottom
    # pages (every invoice seen); on any other the flags would not line up.
    if (
        len(flags) != len(text.split("\n"))
        or "".join(char for char, _ in textmap.tuples) != text
    ):
        flags = []
    return text, flags


def read_pdf(data: bytes) -> PdfText:
    """Return the invoice's text and bold lines, or raise ``ValidationError``.

    A scanned (image-only) PDF yields nothing extractable — say so plainly rather
    than handing an empty string to a parser that would then "not recognise" it.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [_page(page) for page in pdf.pages]
    except Exception:
        raise ValidationError(
            "could not read this PDF — it may be corrupt or not a PDF"
        ) from None
    text = "\n".join(page_text for page_text, _ in pages)
    if not text.strip():
        raise ValidationError(
            "this PDF has no extractable text — a scanned/image invoice can't be "
            "imported, only a text PDF the shop generated"
        )
    # One page without flags leaves the whole invoice without: a partial list
    # would no longer line up with the text.
    bold: tuple[bool, ...] = ()
    if all(flags for _, flags in pages):
        bold = tuple(flag for _, flags in pages for flag in flags)
    return PdfText(text=text, bold_lines=bold)

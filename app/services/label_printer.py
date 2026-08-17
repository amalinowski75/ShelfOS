"""Location labels as bitmaps a label printer can lay on tape (spec §7).

The browser path (``label_service`` + ``labels.html``) renders a label as HTML at
whatever size the print dialog is given. This renders the SAME label onto the
printer's own 300 dpi grid as a 1-bit bitmap, which is what the Brother QL
raster protocol takes. Same QR payload, same text: a label produced either way
scans identically.

Geometry comes from ``brother_ql.labels``, not from a table typed in here. A
tape's printable width is not its physical width (62 mm of tape is 732 dots, of
which 696 print), and the encoder checks a die-cut label's height exactly. A
copied number that drifted would print noise onto real tape.

Sizes are read from ``config`` at call time, so the layout can be tuned against
the PNG preview — reload, look, adjust — without restarting anything or burning
a centimetre of tape per attempt.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import segno
from brother_ql.labels import ALL_LABELS, FormFactor
from PIL import Image, ImageDraw, ImageFont

from app import config
from app.services.errors import ValidationError
from app.services.label_service import LabelData, location_qr_payload

# Every QL model prints at 300 dpi across the tape.
_PX_PER_MM: Final = 300 / 25.4

# Font files to try when none is configured, best first. DejaVu ships with
# almost every Linux desktop (and with GitHub's runners), so the preview a
# developer tunes looks like the label the shelf gets.
_FONT_CANDIDATES: Final = (
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    (
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ),
)

# The QR's quiet zone in modules. The spec says 4; baking it into the rendered
# code (rather than leaving it to the layout) means no future layout change can
# starve it — a QR flush against dark tape edge or text is one that won't scan.
_QR_BORDER: Final = 4

# How much of the label's width the QR may take. Without a cap it would be a
# square as tall as the label, which on a narrow tape leaves nothing for the
# text; with it, a 62 mm tape still gets a ~25 mm code.
_QR_WIDTH_SHARE: Final = 0.45

# A QR whose modules are thinner than this many printer dots (~0.25 mm) is not
# reliably readable by a phone, so a tape that cannot do better is refused
# rather than silently printed as a decorative square.
_MIN_QR_MODULE_PX: Final = 3

# Type scale, in printer dots. The name is what identifies the label across the
# room; the path is context, read close up, and may wrap.
_NAME_MAX_PX: Final = 52
_NAME_MIN_PX: Final = 30
_PATH_MAX_PX: Final = 30
_PATH_MIN_PX: Final = 20
_PATH_MAX_LINES: Final = 3
_PATH_SEPARATOR: Final = " / "

# Sane bounds for a continuous tape's length: below this a QR plus a name does
# not fit, above it a typo would unroll a metre of tape per label.
_MIN_LENGTH_MM: Final = 12.0
_MAX_LENGTH_MM: Final = 300.0


@dataclass(frozen=True)
class TapeGeometry:
    """The pixel canvas one label is drawn on.

    ``width_px`` runs ACROSS the tape and is fixed by the tape itself;
    ``length_px`` runs along the feed and is ours to choose on a continuous
    roll. The bitmap is therefore the label as it comes off the printer.
    """

    tape: str
    width_px: int
    length_px: int
    endless: bool


def _label_spec(tape: str) -> Any:
    """The brother_ql definition of a tape, or a ``ValidationError`` naming it."""
    for spec in ALL_LABELS:
        if spec.identifier == tape:
            return spec
    known = ", ".join(spec.identifier for spec in ALL_LABELS)
    raise ValidationError(f"unknown label tape {tape!r}; known tapes: {known}")


def tape_geometry(
    *, tape: str | None = None, length_mm: float | None = None
) -> TapeGeometry:
    """Canvas size for the configured (or given) tape.

    A die-cut label's length is set by the die, so ``length_mm`` is ignored for
    one — the encoder rejects any other height, and quietly resizing to please
    it would print a label that doesn't match its backing paper.
    """
    tape = config.LABEL_TAPE if tape is None else tape
    spec = _label_spec(tape)
    if spec.form_factor == FormFactor.ROUND_DIE_CUT:
        raise ValidationError(
            f"tape {tape!r} is round; ShelfOS lays a location label out as a "
            "QR beside its path, which needs a rectangular label"
        )
    width_px, die_length_px = spec.dots_printable
    if spec.form_factor == FormFactor.DIE_CUT:
        return TapeGeometry(
            tape=tape, width_px=width_px, length_px=die_length_px, endless=False
        )
    length_mm = config.LABEL_LENGTH_MM if length_mm is None else length_mm
    if not _MIN_LENGTH_MM <= length_mm <= _MAX_LENGTH_MM:
        raise ValidationError(
            f"label length must be between {_MIN_LENGTH_MM:g} and "
            f"{_MAX_LENGTH_MM:g} mm (got {length_mm:g})"
        )
    return TapeGeometry(
        tape=tape,
        width_px=width_px,
        length_px=round(length_mm * _PX_PER_MM),
        endless=True,
    )


def font_paths() -> tuple[str, str]:
    """The (regular, bold) font files to print with.

    Configured paths win; otherwise the first candidate pair that exists. No
    fallback to Pillow's built-in font: it would render at a size nobody chose
    and quietly undo a layout tuned against the preview, which is worse than
    saying plainly that no font was found.
    """
    if config.LABEL_FONT or config.LABEL_FONT_BOLD:
        regular = config.LABEL_FONT or config.LABEL_FONT_BOLD
        bold = config.LABEL_FONT_BOLD or regular
        for path in (regular, bold):
            if not Path(path).is_file():
                raise ValidationError(f"label font {path!r} does not exist")
        return regular, bold
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).is_file() and Path(bold).is_file():
            return regular, bold
    raise ValidationError(
        "no label font found; set SHELFOS_LABEL_FONT to a .ttf file "
        "(and SHELFOS_LABEL_FONT_BOLD, if you want a separate bold)"
    )


@lru_cache(maxsize=8)
def _font_file(path: str) -> bytes:
    return Path(path).read_bytes()


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """A font object, from cached bytes.

    A fresh ``FreeTypeFont`` per call, deliberately: FreeType faces are not safe
    to share across threads, and a preview render can overlap a print. Building
    one from bytes already in memory costs microseconds.
    """
    return ImageFont.truetype(io.BytesIO(_font_file(path)), size)


def _width(text: str, font: ImageFont.FreeTypeFont) -> float:
    return ImageDraw.Draw(Image.new("1", (1, 1))).textlength(text, font=font)


def _ellipsise(text: str, font: ImageFont.FreeTypeFont, box_w: int) -> str:
    """Trim from the END until it fits, marking the cut with an ellipsis."""
    if _width(text, font) <= box_w:
        return text
    for cut in range(len(text) - 1, 0, -1):
        candidate = text[:cut] + "…"
        if _width(candidate, font) <= box_w:
            return candidate
    return "…"


def _wrap(
    text: str, font: ImageFont.FreeTypeFont, box_w: int, separator: str | None
) -> list[str]:
    """Break ``text`` into lines that fit ``box_w``.

    With a ``separator`` the breaks are only ever at separators, so a path's
    segments stay whole — "Rack A" split across two lines reads as two shelves.
    Without one the text is a single line, to be shrunk or ellipsised by the
    caller.
    """
    if separator is None:
        return [text]
    lines: list[str] = []
    current = ""
    for segment in text.split(separator):
        candidate = f"{current}{separator}{segment}" if current else segment
        if current and _width(candidate, font) > box_w:
            lines.append(current)
            current = segment
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fits(
    lines: list[str], font: ImageFont.FreeTypeFont, box_w: int, max_lines: int
) -> bool:
    """Whether wrapped lines fit the box — both count and width."""
    return len(lines) <= max_lines and all(_width(x, font) <= box_w for x in lines)


def fit_lines(
    text: str,
    *,
    font_path: str,
    box_w: int,
    max_px: int,
    min_px: int,
    max_lines: int,
    separator: str | None = None,
) -> tuple[list[str], int]:
    """The exact lines to draw, and the size to draw them at.

    Shrinks the type until the text fits in ``max_lines``; at ``min_px`` it
    gives up on shrinking and drops content instead. What it drops is the
    opinionated part: for a separated path it drops LEADING segments and marks
    the cut ("… / Shelf 02 / D7"). The drawer identifies the label; the room is
    context you already have, standing in it.

    Pure, and returning strings, so the layout rules are testable without
    reading pixels back off a bitmap.
    """
    size = max_px
    while True:
        font = _font(font_path, size)
        lines = _wrap(text, font, box_w, separator)
        if _fits(lines, font, box_w, max_lines):
            return lines, size
        if size <= min_px:
            break
        size = max(min_px, int(size / 1.12))

    font = _font(font_path, min_px)
    if separator is None:
        return [_ellipsise(text, font, box_w)], min_px
    segments = text.split(separator)
    for first in range(1, len(segments)):
        shortened = separator.join(["…", *segments[first:]])
        lines = _wrap(shortened, font, box_w, separator)
        if _fits(lines, font, box_w, max_lines):
            return lines, min_px
    return [_ellipsise(segments[-1], font, box_w)], min_px


def _qr_image(payload: str, box_px: int, tape: str) -> Image.Image:
    """The QR at the largest whole-module size that fits ``box_px``.

    Whole modules only: a fractional scale resamples the code into grey edges
    that a thermal head then thresholds unpredictably.
    """
    qr = segno.make(payload, error="m", micro=False)
    modules = int(qr.symbol_size(scale=1, border=_QR_BORDER)[0])
    scale = box_px // modules
    if scale < _MIN_QR_MODULE_PX:
        raise ValidationError(
            f"tape {tape!r} leaves only {box_px} dots for the QR, "
            f"too small to scan; use a wider tape"
        )
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=scale, border=_QR_BORDER)
    return Image.open(buffer).convert("1")


def render_label(label: LabelData, geometry: TapeGeometry | None = None) -> Image.Image:
    """Draw one location label: QR on the left, name and path beside it."""
    geometry = geometry or tape_geometry()
    margin = round(config.LABEL_MARGIN_MM * _PX_PER_MM)
    box_w = geometry.width_px - 2 * margin
    box_h = geometry.length_px - 2 * margin
    if box_w <= 0 or box_h <= 0:
        raise ValidationError(
            f"a {config.LABEL_MARGIN_MM:g} mm margin leaves no room on tape "
            f"{geometry.tape!r}"
        )

    canvas = Image.new("1", (geometry.width_px, geometry.length_px), 1)
    qr = _qr_image(
        location_qr_payload(label.id),
        min(box_h, round(box_w * _QR_WIDTH_SHARE)),
        geometry.tape,
    )
    canvas.paste(qr, (margin, margin + (box_h - qr.height) // 2))

    gap = margin
    text_x = margin + qr.width + gap
    text_w = geometry.width_px - margin - text_x
    if text_w <= 0:
        raise ValidationError(
            f"tape {geometry.tape!r} leaves no room for text beside the QR"
        )

    regular, bold = font_paths()
    name_lines, name_px = fit_lines(
        label.name,
        font_path=bold,
        box_w=text_w,
        max_px=_NAME_MAX_PX,
        min_px=_NAME_MIN_PX,
        max_lines=1,
    )
    path_lines, path_px = fit_lines(
        label.path,
        font_path=regular,
        box_w=text_w,
        max_px=_PATH_MAX_PX,
        min_px=_PATH_MIN_PX,
        max_lines=_PATH_MAX_LINES,
        separator=_PATH_SEPARATOR,
    )

    # Leading of 1.25 reads better than the fonts' own, which is set for prose.
    name_h = round(name_px * 1.25)
    path_h = round(path_px * 1.25)
    block_h = name_h * len(name_lines) + path_h * len(path_lines)
    y = margin + max(0, (box_h - block_h) // 2)

    draw = ImageDraw.Draw(canvas)
    for line in name_lines:
        draw.text((text_x, y), line, font=_font(bold, name_px), fill=0)
        y += name_h
    for line in path_lines:
        draw.text((text_x, y), line, font=_font(regular, path_px), fill=0)
        y += path_h
    return canvas


def render_png(label: LabelData, geometry: TapeGeometry | None = None) -> bytes:
    """The same bitmap as a PNG — for the preview, and for eyeballing in tests."""
    buffer = io.BytesIO()
    render_label(label, geometry).save(buffer, format="PNG")
    return buffer.getvalue()

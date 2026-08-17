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

import errno
import io
import logging
import os
import select
import stat
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import segno
from brother_ql.conversion import convert
from brother_ql.exceptions import BrotherQLError
from brother_ql.labels import ALL_LABELS, Color, FormFactor
from brother_ql.raster import BrotherQLRaster
from PIL import Image, ImageDraw, ImageFont

from app import config
from app.services.errors import PrinterError, ValidationError
from app.services.label_service import LabelData, location_qr_payload

_logger = logging.getLogger("shelfos")

# One printer, so one job at a time. Process-wide: running more than one worker
# process would need the printer's own EBUSY as the backstop, which is why
# ShelfOS is documented as a single-process deployment.
_PRINT_LOCK = threading.Lock()

# How often to look at the device while waiting on it, and how much to hand the
# kernel at a time.
_POLL_SECONDS: Final = 0.2
# How long a status question waits for the printer to be free. Short: a preview
# asking what tape is loaded must never sit behind a whole print job.
_STATUS_LOCK_SECONDS: Final = 1.0
_CHUNK_BYTES: Final = 4096
# How long one label may take to come out before the wait for confirmation is
# given up on (the job is still printing; only the confirmation is abandoned).
_CONFIRM_SECONDS_PER_LABEL: Final = 3.0

_NOT_CONFIGURED: Final = (
    "label printing is not configured; set SHELFOS_LABEL_DEVICE to the "
    "printer's device (usually /dev/usb/lp0)"
)

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

# How much of the label the QR may take, per arrangement. Without a cap it
# would be a square as large as the short side, leaving nothing for the text;
# with it, a 62 mm tape still gets a ~25 mm code.
_QR_WIDTH_SHARE: Final = 0.45
_QR_HEIGHT_SHARE: Final = 0.55

# A ceiling in millimetres, because a QR stops improving once it is comfortably
# scannable: 25 mm of version-1 code reads across a room, and every millimetre
# past that is tape spent on nothing. The same reasoning holds the type sizes
# fixed — a big label gets white space, not a giant everything.
_QR_MAX_MM: Final = 25.0

# How much wider than tall a label must be before the QR goes beside the text
# rather than above it. Below this the side-by-side split starves both.
_SIDE_BY_SIDE_RATIO: Final = 1.3

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
    # The tape's physical width, which is what the printer reports about itself
    # — 62 mm of tape is 696 PRINTABLE dots, so the two numbers never match.
    width_mm: int
    length_px: int
    endless: bool
    # Black/red tape (DK-22251 and friends). Not a feature — a requirement: a
    # QL-800 with two-colour tape loaded REFUSES a one-colour job, reporting an
    # error with no error bits set, which reads like a broken printer rather
    # than a mismatched job. The red layer may well be empty; what matters is
    # that the job is shaped for the tape that is in the machine.
    two_color: bool


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
    width_mm = int(spec.tape_size[0])
    two_color = spec.color == Color.BLACK_RED_WHITE
    if spec.form_factor == FormFactor.DIE_CUT:
        return TapeGeometry(
            tape=tape,
            width_px=width_px,
            width_mm=width_mm,
            length_px=die_length_px,
            endless=False,
            two_color=two_color,
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
        width_mm=width_mm,
        length_px=round(length_mm * _PX_PER_MM),
        endless=True,
        two_color=two_color,
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


def _qr_code(payload: str) -> segno.QRCode:
    """The code itself, before it is drawn at any size.

    ``error="m"`` is a FLOOR, not the level used: segno's ``boost_error`` raises
    it to the highest that still fits the chosen version, and an ``SL<id>``
    payload leaves a version-1 symbol with room for H. That is the level we
    want on thermal tape that gets thumbed and scuffed, so the boost is kept —
    named here because the argument alone reads as if M were the outcome.

    ``micro=False``: segno would happily emit a Micro QR for a payload this
    short, and phone cameras are unreliable with those.
    """
    return segno.make(payload, error="m", micro=False)


def _qr_image(payload: str, box_px: int, tape: str) -> Image.Image:
    """The QR at the largest whole-module size that fits ``box_px``.

    Whole modules only: a fractional scale resamples the code into grey edges
    that a thermal head then thresholds unpredictably.
    """
    qr = _qr_code(payload)
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


def _layout(box_w: int, box_h: int) -> tuple[bool, int]:
    """Whether the QR sits BESIDE the text, and how big it may be.

    A label's proportions are set by the tape, and one arrangement does not
    serve both: on a 62 x 30 mm strip the QR belongs to the left of the text,
    while on a 29 mm roll the same rule leaves a stamp-sized code and a column
    two characters wide. So a label that is clearly wider than tall gets the
    side-by-side layout, and anything squarer or taller stacks the code above
    the text, where each gets the full width.
    """
    ceiling = round(_QR_MAX_MM * _PX_PER_MM)
    beside = box_w >= box_h * _SIDE_BY_SIDE_RATIO
    if beside:
        return True, min(box_h, round(box_w * _QR_WIDTH_SHARE), ceiling)
    return False, min(box_w, round(box_h * _QR_HEIGHT_SHARE), ceiling)


def _text_block(
    label: LabelData, box_w: int, box_h: int
) -> tuple[list[tuple[str, str, int]], int]:
    """The lines to draw as (text, font path, size), and how tall they stack.

    Fitted to BOTH dimensions. Shrinking to the width alone was enough for a
    30 mm label and wrong for a short one: a five-segment path wrapped to three
    lines and the last of them printed past the end of the tape, guillotined by
    the cutter — the one way this module could quietly produce a bad label,
    while refusing round tapes, thin QR modules and absurd lengths outright.

    So the name is capped by the height it is given, and the path gets the lines
    that are left over. Measuring it here rather than while drawing also lets a
    layout centre the code and the text together, instead of centring each in
    its own half and leaving a hole between them on a long label.
    """
    regular, bold = font_paths()
    # Leading of 1.25 reads better than the fonts' own, which is set for prose.
    name_lines, name_px = fit_lines(
        label.name,
        font_path=bold,
        box_w=box_w,
        max_px=min(_NAME_MAX_PX, max(_NAME_MIN_PX, int(box_h / 1.25))),
        min_px=_NAME_MIN_PX,
        max_lines=1,
    )
    name_h = round(name_px * 1.25)
    lines = [(line, bold, name_px) for line in name_lines]
    height = name_h * len(name_lines)

    room = box_h - height
    path_lines: list[str] = []
    path_px = _PATH_MIN_PX
    # Fit, then check what the fitted size really costs: shrinking may leave
    # room for a line the first estimate ruled out, and growing never happens.
    allowed = min(_PATH_MAX_LINES, int(room // round(_PATH_MIN_PX * 1.25)))
    while allowed > 0:
        path_lines, path_px = fit_lines(
            label.path,
            font_path=regular,
            box_w=box_w,
            max_px=_PATH_MAX_PX,
            min_px=_PATH_MIN_PX,
            max_lines=allowed,
            separator=_PATH_SEPARATOR,
        )
        if len(path_lines) * round(path_px * 1.25) <= room:
            break
        allowed -= 1
        path_lines = []
    lines += [(line, regular, path_px) for line in path_lines]
    return lines, height + len(path_lines) * round(path_px * 1.25)


def render_label(label: LabelData, geometry: TapeGeometry | None = None) -> Image.Image:
    """Draw one location label, arranged to suit the tape it will print on."""
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
    beside, qr_box = _layout(box_w, box_h)
    qr = _qr_image(location_qr_payload(label.id), qr_box, geometry.tape)
    gap = margin

    text_w = (box_w - qr.width - gap) if beside else box_w
    if text_w <= 0:
        raise ValidationError(
            f"tape {geometry.tape!r} leaves no room for text beside the QR"
        )
    text_room = box_h if beside else box_h - qr.height - gap
    lines, text_h = _text_block(label, text_w, text_room)

    if beside:
        canvas.paste(qr, (margin, margin + (box_h - qr.height) // 2))
        text_x = margin + qr.width + gap
        text_y = margin + max(0, (box_h - text_h) // 2)
    else:
        # Code and text are centred as one block, so the pair sits together
        # wherever the tape leaves spare length.
        stack_h = qr.height + gap + text_h
        top = margin + max(0, (box_h - stack_h) // 2)
        canvas.paste(qr, (margin + (box_w - qr.width) // 2, top))
        text_x, text_y = margin, top + qr.height + gap

    draw = ImageDraw.Draw(canvas)
    for text, font_path, size in lines:
        draw.text((text_x, text_y), text, font=_font(font_path, size), fill=0)
        text_y += round(size * 1.25)
    return canvas


def render_png(label: LabelData, geometry: TapeGeometry | None = None) -> bytes:
    """The same bitmap as a PNG — for the preview, and for eyeballing in tests."""
    buffer = io.BytesIO()
    render_label(label, geometry).save(buffer, format="PNG")
    return buffer.getvalue()


# --- Talking to the printer --------------------------------------------------
#
# The raster bytes go straight to a device path (``/dev/usb/lp0``) rather than
# through brother_ql's USB backend or CUPS. It needs no libusb, it fails with an
# errno that can be turned into a sentence worth reading — and a test can point
# the device at a temporary file, so the whole path, real encoder included, runs
# in CI with no printer and no mocks.
#
# That device is BIDIRECTIONAL: ask a QL for its status and it answers with 32
# bytes naming the tape it holds, the phase it is in, and what went wrong. This
# was written off as impossible at first, on the theory that a one-way write
# cannot know anything; the first evening with real hardware disproved that, and
# spent an hour on a diagnosis the printer would have handed over in one frame.
# So a job is checked before it is sent and confirmed after.
#
# Everything about the readback is best-effort. A device that answers nothing —
# a plain file in the tests, a printer whose firmware stays quiet — leaves the
# behaviour exactly as it was: send, and report the job as sent rather than
# printed. Nothing here may turn silence into a failure.

_STATUS_REQUEST: Final = b"\x1b\x69\x53"
_STATUS_LEN: Final = 32
_STATUS_MARK: Final = b"\x80\x20"  # every frame starts with these two bytes

# Error information 1 and 2, bit by bit, in the printer's own terms.
_ERRORS_1: Final = (
    "no tape in the printer",
    "the tape has run out",
    "the cutter is jammed",
    "",
    "the printer is busy",
    "the printer is off",
    "power adapter fault",
    "fan fault",
)
_ERRORS_2: Final = (
    "the wrong tape is loaded",
    "the printer's buffer is full",
    "a communication error",
    "the printer's buffer is full",
    "the cover is open",
    "the job was cancelled",
    "the tape cannot be fed",
    "a printer system error",
)

# Media types the printer distinguishes. Note what is NOT here: the roll's
# colour capability. A black/red DK-22251 reports itself exactly like a plain
# white roll (media type 0x0A, "number of colors" 0, tape colour 0), which is
# why a one-colour job on two-colour tape can only be diagnosed after it is
# refused — see _await_completion.
_MEDIA_CONTINUOUS: Final = 0x0A
_MEDIA_DIE_CUT: Final = 0x0B

_STATUS_REPLY: Final = 0x00
_STATUS_COMPLETED: Final = 0x01
_STATUS_ERROR: Final = 0x02


@dataclass(frozen=True)
class PrinterStatus:
    """What the printer says about itself, decoded from one status frame."""

    media_width_mm: int
    media_length_mm: int
    media_type: int
    errors: tuple[str, ...]
    status_type: int
    phase: int

    @property
    def has_tape(self) -> bool:
        return self.media_type != 0x00


def _decode_status(frame: bytes) -> PrinterStatus | None:
    """Decode a 32-byte status frame, or ``None`` if it is not one."""
    if len(frame) < _STATUS_LEN or not frame.startswith(_STATUS_MARK):
        return None
    errors = tuple(
        phrase
        for byte, phrases in ((frame[8], _ERRORS_1), (frame[9], _ERRORS_2))
        for bit, phrase in enumerate(phrases)
        if phrase and byte & (1 << bit)
    )
    return PrinterStatus(
        media_width_mm=frame[10],
        # Zero on a continuous roll, the die's length on a die-cut one.
        media_length_mm=frame[17],
        media_type=frame[11],
        errors=errors,
        status_type=frame[18],
        phase=frame[19],
    )


def _open_device(device: str) -> int:
    """Open the printer, translating errno into something actionable."""
    try:
        return os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except FileNotFoundError:
        raise PrinterError(
            f"the label printer is not there ({device} does not exist) — "
            "check that it is plugged in and switched on"
        ) from None
    except PermissionError:
        raise PrinterError(
            f"no permission to use {device} — add the ShelfOS user to the 'lp' "
            "group, or install a udev rule for the printer"
        ) from None
    except OSError as error:
        raise PrinterError(f"could not open {device}: {error.strerror}") from None


def _write_all(fd: int, data: bytes, device: str, budget: float) -> None:
    """Write the whole job, or give up — never block a worker thread forever.

    A blocking write to a printer that has stopped draining its endpoint hangs
    with no timeout of any kind; that happened on the first evening with real
    hardware and wedged the process until it was killed.
    """
    sent = 0
    deadline = time.monotonic() + budget
    while sent < len(data):
        if time.monotonic() >= deadline:
            raise PrinterError(
                "the printer stopped accepting the job — it is usually waiting "
                "for an error to be cleared with the button on the front"
            )
        if not select.select([], [fd], [], _POLL_SECONDS)[1]:
            continue
        try:
            sent += os.write(fd, data[sent : sent + _CHUNK_BYTES])
        except BlockingIOError:
            time.sleep(_POLL_SECONDS)
        except OSError as error:
            if error.errno == errno.EBUSY:
                raise PrinterError(
                    f"{device} is held by another program — CUPS usually is the "
                    "one; remove its queue with 'lpadmin -x'"
                ) from None
            if error.errno == errno.ENODEV:
                # CUPS's usb backend detaches the kernel driver while it talks to
                # the printer, so a queue for this printer makes the node vanish
                # from under a job. Seen in dmesg as "usblp4: removed".
                raise PrinterError(
                    f"{device} disappeared mid-job — another program (usually "
                    "CUPS, which detaches the kernel driver) is using the "
                    "printer; remove its queue with 'lpadmin -x'"
                ) from None
            _logger.warning("label print to %s failed: %s", device, error)
            raise PrinterError("the label printer did not accept the job") from None


def _answers_questions(fd: int) -> bool:
    """Whether this device can be asked anything.

    Only a character device is a printer. A regular file — the tests' stand-in,
    or a misconfigured path — reports itself readable and then returns nothing
    for ever, so asking it would burn the whole timeout on every print.
    """
    return stat.S_ISCHR(os.fstat(fd).st_mode)


def _read_status(fd: int, budget: float) -> PrinterStatus | None:
    """Wait for one status frame, or ``None`` if the device stays quiet."""
    buffer = b""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], _POLL_SECONDS)[0]:
            continue
        try:
            chunk = os.read(fd, 64)
        except (BlockingIOError, OSError):
            return None
        if not chunk:
            time.sleep(_POLL_SECONDS)
            continue
        buffer += chunk
        while len(buffer) >= _STATUS_LEN:
            status = _decode_status(buffer[:_STATUS_LEN])
            buffer = buffer[_STATUS_LEN:]
            if status is not None:
                return status
    return None


def read_printer_status(device: str | None = None) -> PrinterStatus | None:
    """Ask the printer how it is, or ``None`` if it does not answer."""
    device = config.LABEL_DEVICE if device is None else device
    if not device:
        raise ValidationError(_NOT_CONFIGURED)
    fd = _open_device(device)
    try:
        if not _answers_questions(fd):
            return None
        _write_all(fd, _STATUS_REQUEST, device, budget=config.LABEL_STATUS_TIMEOUT)
        return _read_status(fd, budget=config.LABEL_STATUS_TIMEOUT)
    finally:
        os.close(fd)


def detect_tape(status: PrinterStatus) -> str | None:
    """The tape identifier matching what the printer says it holds.

    Width and continuous-versus-die-cut come straight from the status frame,
    and a die-cut roll reports its length too — together that is enough to name
    the tape without anybody configuring it. What the frame cannot say is
    whether the roll is the black/red kind, so when the configured tape fits
    the reported geometry it wins: the printer knows the size, the human knows
    the ink.
    """
    endless = status.media_type == _MEDIA_CONTINUOUS
    if status.media_type not in (_MEDIA_CONTINUOUS, _MEDIA_DIE_CUT):
        return None
    matches = [
        spec.identifier
        for spec in ALL_LABELS
        if spec.tape_size[0] == status.media_width_mm
        and (spec.form_factor == FormFactor.ENDLESS) == endless
        and (endless or spec.tape_size[1] == status.media_length_mm)
    ]
    if not matches:
        return None
    return config.LABEL_TAPE if config.LABEL_TAPE in matches else matches[0]


def _geometry_for(status: PrinterStatus | None) -> TapeGeometry:
    """The geometry to render for: the printer's tape when it says, else the
    configured one."""
    if status is not None:
        detected = detect_tape(status)
        if detected is not None:
            if detected != config.LABEL_TAPE:
                _logger.info(
                    "printing on %s tape (the printer's), not the configured %s",
                    detected,
                    config.LABEL_TAPE,
                )
            return tape_geometry(tape=detected)
    return tape_geometry()


def resolve_geometry(device: str | None = None) -> TapeGeometry:
    """What the next label will be laid out for — asking the printer if it can.

    Best-effort by construction: a printer that is unplugged, busy or silent
    just leaves the configured tape in charge, so a preview still renders with
    no printer in the building.
    """
    device = config.LABEL_DEVICE if device is None else device
    if not device:
        return tape_geometry()
    if not _PRINT_LOCK.acquire(timeout=_STATUS_LOCK_SECONDS):
        return tape_geometry()  # a print is in flight; do not interrupt it
    try:
        return _geometry_for(read_printer_status(device))
    except (PrinterError, ValidationError):
        return tape_geometry()
    finally:
        _PRINT_LOCK.release()


def _refuse_if_not_ready(status: PrinterStatus, geometry: TapeGeometry) -> None:
    """Stop before printing when the printer already knows it cannot."""
    if status.errors:
        raise PrinterError("the printer reports: " + "; ".join(status.errors))
    if not status.has_tape:
        raise PrinterError("there is no tape in the printer")
    if status.media_width_mm != geometry.width_mm:
        # Only reachable when the tape could not be recognised (detect_tape
        # found nothing), since otherwise the layout follows the printer.
        raise ValidationError(
            f"the printer has {status.media_width_mm} mm tape loaded, which "
            f"matches no tape ShelfOS knows; SHELFOS_LABEL_TAPE is "
            f"{geometry.tape!r} ({geometry.width_mm} mm)"
        )


def _await_completion(fd: int, budget: float) -> bool:
    """Whether the printer confirmed the job; raise if it reported failure."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        status = _read_status(fd, budget=max(0.0, deadline - time.monotonic()))
        if status is None:
            return False
        if status.status_type == _STATUS_COMPLETED:
            return True
        if status.status_type == _STATUS_ERROR:
            if status.errors:
                raise PrinterError("the printer stopped: " + "; ".join(status.errors))
            # An error with no reason given. Seen exactly once, and it cost an
            # evening: a QL-800 refuses a one-colour job when black/red tape is
            # loaded, and says nothing more than this about it.
            raise PrinterError(
                "the printer refused the job without saying why — the usual "
                "cause is a tape it cannot print that job on: black/red tape "
                "(DK-22251) needs SHELFOS_LABEL_TAPE=62red, plain tape needs 62"
            )
    return False


@dataclass(frozen=True)
class PrintOutcome:
    """How many labels went, whether the printer confirmed, and on what tape."""

    sent: int
    confirmed: bool
    tape: str


def _print_job(labels: Sequence[LabelData], geometry: TapeGeometry) -> bytes:
    """Encode labels as one Brother QL raster job."""
    raster = BrotherQLRaster(config.LABEL_PRINTER_MODEL)
    # Say something rather than emitting a job the printer will quietly discard.
    raster.exception_on_warning = True
    images = [render_label(label, geometry) for label in labels]
    if geometry.two_color:
        # Two-colour tape wants two raster planes, which the encoder splits out
        # of an RGB image by hue. Ours is black-on-white, so the red plane comes
        # out empty — that is fine, and the printer will not take the job in any
        # other shape.
        images = [image.convert("RGB") for image in images]
    try:
        data = convert(
            raster, images, geometry.tape, rotate=0, cut=True, red=geometry.two_color
        )
    except (ValueError, LookupError, BrotherQLError) as error:
        raise ValidationError(f"could not build the print job: {error}") from None
    return bytes(data)


def print_labels(
    labels: Sequence[LabelData],
    *,
    copies: int = 1,
    device: str | None = None,
) -> PrintOutcome:
    """Print labels, and say whether the printer confirmed doing so.

    ``device`` overrides the configured path — the injection point tests use,
    pointing it at a temporary file so the encoder and the write both really
    run. Jobs are serialised on a process-wide lock: there is one printer, and
    two interleaved raster streams would print one ruined label.
    """
    device = config.LABEL_DEVICE if device is None else device
    if not device:
        raise ValidationError(_NOT_CONFIGURED)
    if copies < 1:
        raise ValidationError("copies must be at least 1")
    if not labels:
        raise ValidationError("nothing to print")
    total = len(labels) * copies
    if total > config.LABEL_MAX_JOB:
        raise ValidationError(
            f"at most {config.LABEL_MAX_JOB} labels per print job (asked for "
            f"{total}); print a smaller branch"
        )

    # Clamped: Lock.acquire treats a negative timeout as "wait for ever" (and
    # raises below -1), so a typo in the setting would hold a worker thread
    # exactly as long as the setting's own comment says it must not.
    if not _PRINT_LOCK.acquire(timeout=max(0.0, config.LABEL_PRINT_TIMEOUT)):
        raise PrinterError("the label printer is busy — try again in a moment")
    try:
        fd = _open_device(device)
        try:
            asks = _answers_questions(fd)
            before = None
            if asks:
                _write_all(fd, _STATUS_REQUEST, device, config.LABEL_STATUS_TIMEOUT)
                before = _read_status(fd, budget=config.LABEL_STATUS_TIMEOUT)
            # The tape in the machine decides the label's size and shape; the
            # encoding therefore happens here, once the printer has answered.
            geometry = _geometry_for(before)
            if before is not None:
                _refuse_if_not_ready(before, geometry)
            data = _print_job(
                [label for label in labels for _ in range(copies)], geometry
            )
            _write_all(fd, data, device, budget=config.LABEL_PRINT_TIMEOUT)
            # A silent printer must not hold the request open for the whole
            # print timeout, so the wait is scaled to the job and capped by it.
            confirmed = asks and _await_completion(
                fd,
                budget=min(
                    config.LABEL_PRINT_TIMEOUT,
                    config.LABEL_STATUS_TIMEOUT + _CONFIRM_SECONDS_PER_LABEL * total,
                ),
            )
        finally:
            os.close(fd)
    finally:
        _PRINT_LOCK.release()
    _logger.info(
        "%s %d label(s) on %s tape to %s",
        "printed" if confirmed else "sent",
        total,
        geometry.tape,
        device,
    )
    return PrintOutcome(sent=total, confirmed=confirmed, tape=geometry.tape)

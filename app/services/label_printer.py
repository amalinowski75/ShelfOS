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


def _refuse_if_not_ready(status: PrinterStatus, geometry: TapeGeometry) -> None:
    """Stop before printing when the printer already knows it cannot."""
    if status.errors:
        raise PrinterError("the printer reports: " + "; ".join(status.errors))
    if not status.has_tape:
        raise PrinterError("there is no tape in the printer")
    if status.media_width_mm != geometry.width_mm:
        raise ValidationError(
            f"the printer has {status.media_width_mm} mm tape loaded, but "
            f"SHELFOS_LABEL_TAPE is {geometry.tape!r} ({geometry.width_mm} mm)"
        )
    loaded_endless = status.media_type == _MEDIA_CONTINUOUS
    if status.media_type in (_MEDIA_CONTINUOUS, _MEDIA_DIE_CUT) and (
        loaded_endless != geometry.endless
    ):
        # The other half of what the printer can actually tell us. A die-cut job
        # on a continuous roll prints across the gaps; the reverse is refused by
        # the encoder with a raw pixel count, which explains nothing.
        loaded = "continuous" if loaded_endless else "die-cut"
        wanted = "continuous" if geometry.endless else "die-cut"
        raise ValidationError(
            f"the printer has {loaded} tape loaded, but SHELFOS_LABEL_TAPE is "
            f"{geometry.tape!r}, which is {wanted}"
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
    """How many labels went, and whether the printer said it printed them."""

    sent: int
    confirmed: bool


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

    geometry = tape_geometry()
    data = _print_job([label for label in labels for _ in range(copies)], geometry)
    if not _PRINT_LOCK.acquire(timeout=config.LABEL_PRINT_TIMEOUT):
        raise PrinterError("the label printer is busy — try again in a moment")
    try:
        fd = _open_device(device)
        try:
            asks = _answers_questions(fd)
            if asks:
                _write_all(fd, _STATUS_REQUEST, device, config.LABEL_STATUS_TIMEOUT)
                before = _read_status(fd, budget=config.LABEL_STATUS_TIMEOUT)
                if before is not None:
                    _refuse_if_not_ready(before, geometry)
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
        "%s %d label(s) to %s", "printed" if confirmed else "sent", total, device
    )
    return PrintOutcome(sent=total, confirmed=confirmed)

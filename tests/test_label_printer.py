"""Tests for rendering a location label onto a printer's pixel grid (spec §7).

The layout rules are asserted through :func:`fit_lines`, which returns the exact
strings it would draw — so what gets checked here is the rule, not a bitmap that
would need reading back with OCR. The bitmap itself is checked for the things
that are actually about pixels: canvas size, margins, and that the QR sits where
the layout says at the scale the layout chose.
"""

from __future__ import annotations

import io
import os
import threading

import pytest
import segno
from app import config
from app.services import label_printer as lp
from app.services.errors import PrinterError, ValidationError
from app.services.label_service import LabelData, location_qr_payload
from PIL import Image, ImageChops

from tests.fake_printer import IDLE_FRAME, FakePrinter, frame

_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _label(
    name: str = "Drawer 03", path: str = "Lab / Rack A / Drawer 03"
) -> LabelData:
    return LabelData(id=123, name=name, path=path, qr_svg="")


def _ink_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Where the black pixels are (Pillow's bbox is of the non-zero channel)."""
    box = ImageChops.invert(image.convert("L")).getbbox()
    return box  # type: ignore[no-any-return]


def test_default_geometry_is_the_62mm_roll() -> None:
    geometry = lp.tape_geometry()
    # 62 mm of tape is 732 dots wide, of which 696 print — the printable number
    # is the one that matters, and it comes from brother_ql, not from arithmetic.
    assert (geometry.width_px, geometry.tape) == (696, "62")
    assert geometry.endless
    assert geometry.length_px == round(30 * 300 / 25.4) == 354


def test_die_cut_tape_ignores_the_configured_length(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "LABEL_LENGTH_MM", 90.0)
    geometry = lp.tape_geometry(tape="62x29")
    # The die fixes the length; the encoder rejects any other height, so a
    # length setting must not be able to produce one.
    assert (geometry.width_px, geometry.length_px) == (696, 271)
    assert not geometry.endless


def test_unknown_and_round_tapes_are_refused() -> None:
    with pytest.raises(ValidationError, match="unknown label tape"):
        lp.tape_geometry(tape="nonsense")
    # A round die-cut is a real Brother tape, but not one this layout fits.
    with pytest.raises(ValidationError, match="round"):
        lp.tape_geometry(tape="d24")


def test_absurd_lengths_are_refused() -> None:
    with pytest.raises(ValidationError, match="between"):
        lp.tape_geometry(length_mm=2)
    with pytest.raises(ValidationError, match="between"):
        lp.tape_geometry(length_mm=5000)


def test_render_fills_the_tape_and_keeps_the_margin() -> None:
    image = lp.render_label(_label())
    assert image.size == (696, 354)
    assert image.mode == "1"  # what the raster encoder wants: one bit per dot
    margin = round(config.LABEL_MARGIN_MM * 300 / 25.4)
    box = _ink_box(image)
    assert box is not None
    left, top, right, bottom = box
    assert left >= margin and top >= margin
    assert right <= image.width - margin and bottom <= image.height - margin


def test_qr_is_placed_whole_and_unscaled() -> None:
    """The printed QR must be byte-identical to the code segno generates.

    Cheaper and stricter than decoding it: it proves the payload, the module
    scale and the quiet zone all survived the layout, with no image decoder
    dependency. A resampled QR (fractional scale) would fail this immediately.
    """
    label = _label()
    image = lp.render_label(label)
    expected_bytes = io.BytesIO()
    segno.make(location_qr_payload(label.id), error="m", micro=False).save(
        expected_bytes, kind="png", scale=10, border=4
    )
    expected = Image.open(expected_bytes).convert("1")
    margin = round(config.LABEL_MARGIN_MM * 300 / 25.4)
    top = margin + ((354 - 2 * margin) - expected.height) // 2
    printed = image.crop((margin, top, margin + expected.width, top + expected.height))
    assert printed.tobytes() == expected.tobytes()


def test_a_narrow_tape_gives_up_margin_before_readability() -> None:
    """On 12 mm tape a 2 mm border would eat nearly half the printable width
    and leave the code unscannable. The border is what yields — down to a
    hairline, never past the point where the modules get too thin."""
    geometry = lp.tape_geometry(tape="12")
    canvas_w, canvas_h, _ = lp._drawing_size(geometry)
    margin = lp._margin_for(canvas_w, canvas_h)
    wanted = round(config.LABEL_MARGIN_MM * 300 / 25.4)

    assert lp._MIN_MARGIN_PX <= margin < wanted  # shrunk, but not to nothing
    box = lp._layout(canvas_w - 2 * margin, canvas_h - 2 * margin)[1]
    assert box // lp._qr_modules() >= lp._MIN_QR_MODULE_PX
    # And it gives up no more than it must: one dot more would be too much.
    bigger = lp._layout(canvas_w - 2 * (margin + 1), canvas_h - 2 * (margin + 1))[1]
    assert bigger // lp._qr_modules() < lp._MIN_QR_MODULE_PX

    lp.render_label(_label(), geometry)  # and it renders, which is the point


def test_a_long_narrow_tape_is_composed_along_its_length() -> None:
    """A name laid ACROSS 12 mm of tape degenerates into "Dr…"; along it, the
    whole thing fits. So a label taller than it is wide is drawn in landscape
    and turned a quarter turn, which is how such a roll is read anyway."""
    geometry = lp.tape_geometry(tape="12")
    assert lp._drawing_size(geometry) == (geometry.length_px, geometry.width_px, True)
    # A 62 x 30 mm strip is already landscape and stays put.
    wide = lp.tape_geometry(tape="62")
    assert lp._drawing_size(wide) == (wide.width_px, wide.length_px, False)

    image = lp.render_label(_label(), geometry)
    # The raster still wants the tape's own dimensions, turned or not.
    assert image.size == (geometry.width_px, geometry.length_px)
    # Turn it back and the writing is wider than the tape — which is only
    # possible if it was composed along the length rather than across it.
    composed = image.transpose(Image.Transpose.ROTATE_270)
    box = _ink_box(composed)
    assert box is not None
    assert box[2] - box[0] > geometry.width_px


def _ink_per_quarter(image: Image.Image) -> list[int]:
    """How much ink each quarter of the label holds, top to bottom.

    The QR is the dense part of any label, so this says which end it is on —
    which is the only way to tell a quarter turn from the opposite one without
    a printed label in hand.
    """
    height = image.height // 4
    quarters = []
    for index in range(4):
        band = image.crop((0, index * height, image.width, (index + 1) * height))
        quarters.append(sum(1 for pixel in band.convert("L").tobytes() if pixel < 128))
    return quarters


def test_two_die_cut_rolls_of_the_same_width_are_not_the_same_roll() -> None:
    """62 x 29 and 62 x 100 are both 62 mm and both die-cut; only the die tells
    them apart, so a job for one must not print itself onto the other."""
    small = lp.tape_geometry(tape="62x29")
    large = lp.tape_geometry(tape="62x100")
    assert not lp._same_roll(small, large)
    # A continuous roll's length is ours to choose, so length is not part of
    # its identity — 62 and 62red are the same roll to a printer.
    assert lp._same_roll(lp.tape_geometry(tape="62"), lp.tape_geometry(tape="62red"))
    assert not lp._same_roll(lp.tape_geometry(tape="62"), small)


def test_the_turn_puts_the_code_at_the_end_that_leaves_the_printer_first() -> None:
    """Which way the label is turned decides whether it reads upright, and that
    is not something code review catches — it is a roll of tape.

    The code is composed at the LEFT of the landscape canvas, so an
    anti-clockwise turn must leave it at the BOTTOM of the printed label.
    """
    quarters = _ink_per_quarter(lp.render_label(_label(), lp.tape_geometry(tape="12")))
    # Turned the other way the whole distribution mirrors, so both ends of this
    # comparison move — which is what makes it a direction test and not a
    # coincidence about where text happens to be dense.
    assert quarters[-1] == max(quarters), quarters
    assert quarters[0] == min(quarters), quarters


def test_a_tape_with_no_room_for_a_readable_code_is_refused() -> None:
    """No Brother tape is this narrow, which is why the guard needs a synthetic
    one: it is the last line between a decorative square and a refusal."""
    unprintable = lp.TapeGeometry(
        tape="hypothetical",
        width_px=80,
        width_mm=7,
        length_px=354,
        endless=True,
        two_color=False,
    )
    assert not lp._is_printable(unprintable)
    with pytest.raises(ValidationError, match="too small to scan"):
        lp.render_label(_label(), unprintable)


def test_the_qr_is_built_at_the_strongest_correction_that_fits() -> None:
    """What segno actually produces, not what the argument reads like.

    ``error="m"`` is a floor; boost_error raises it to H for a payload this
    short. Asserted here rather than inferred from the call, because the test
    that compares rendered pixels builds its expectation the same way and would
    move with the source.
    """
    code = lp._qr_code(location_qr_payload(123))
    assert code.error == "H"
    assert code.version == 1
    assert code.mode == "alphanumeric"


def test_path_wraps_on_separators_only() -> None:
    lines, size = lp.fit_lines(
        "Lab / Rack A / Shelf 02 / Drawer 03",
        font_path=_DEJAVU,
        box_w=318,
        max_px=30,
        min_px=20,
        max_lines=3,
        separator=" / ",
    )
    assert len(lines) <= 3
    assert size <= 30
    # Every segment survives whole: "Rack A" split across lines would read as
    # two different racks.
    assert "".join(lines).replace(" / ", "") == "LabRack AShelf 02Drawer 03"


def test_a_path_too_long_to_shrink_drops_leading_segments() -> None:
    lines, size = lp.fit_lines(
        " / ".join(f"Level {n}" for n in range(1, 13)),
        font_path=_DEJAVU,
        box_w=318,
        max_px=30,
        min_px=20,
        max_lines=3,
        separator=" / ",
    )
    assert size == 20  # shrunk as far as it may before dropping anything
    assert len(lines) <= 3
    assert lines[0].startswith("…")
    # The tail is what identifies the label: the room is context you already
    # have, standing in it; the drawer is not.
    assert lines[-1].endswith("Level 12")


def test_a_name_too_long_shrinks_then_ellipsises() -> None:
    lines, size = lp.fit_lines(
        "Werkstattschrank-Unterschublade-17",
        font_path=_DEJAVU,
        box_w=318,
        max_px=52,
        min_px=30,
        max_lines=1,
    )
    assert size == 30
    assert lines == [lines[0]] and lines[0].endswith("…")
    assert lines[0].startswith("Werkstatt")  # trimmed from the end, not the start


def test_short_text_is_left_alone_at_full_size() -> None:
    lines, size = lp.fit_lines(
        "D1", font_path=_DEJAVU, box_w=318, max_px=52, min_px=30, max_lines=1
    )
    assert (lines, size) == (["D1"], 52)


def test_font_paths_prefers_configuration_and_reports_a_missing_file(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(config, "LABEL_FONT", _DEJAVU)
    monkeypatch.setattr(config, "LABEL_FONT_BOLD", "")
    # One configured font is enough — it is used for both roles.
    assert lp.font_paths() == (_DEJAVU, _DEJAVU)

    monkeypatch.setattr(config, "LABEL_FONT", "/nowhere/Nope.ttf")
    with pytest.raises(ValidationError, match="does not exist"):
        lp.font_paths()


def test_no_font_anywhere_says_what_to_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "LABEL_FONT", "")
    monkeypatch.setattr(config, "LABEL_FONT_BOLD", "")
    monkeypatch.setattr(lp, "_FONT_CANDIDATES", ())
    # Not a silent fallback to Pillow's built-in font: that would render at a
    # size nobody chose and quietly undo a layout tuned against the preview.
    with pytest.raises(ValidationError, match="SHELFOS_LABEL_FONT"):
        lp.font_paths()


def test_render_png_is_a_png_of_the_same_bitmap() -> None:
    data = lp.render_png(_label())
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert Image.open(io.BytesIO(data)).size == (696, 354)


def test_unusable_label_settings_are_reported_at_startup(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """A bad tape would otherwise first show up at the printer, roll in hand."""
    from app.main import _check_label_settings

    monkeypatch.setattr(config, "LABEL_TAPE", "nope")
    with caplog.at_level("WARNING", logger="shelfos"):
        _check_label_settings()
    assert "unknown label tape" in caplog.text

    # A die-cut tape's length comes from the die, so a length setting does
    # nothing — and silence there is indistinguishable from it having worked.
    caplog.clear()
    monkeypatch.setattr(config, "LABEL_TAPE", "62x29")
    monkeypatch.setenv("SHELFOS_LABEL_LENGTH_MM", "90")
    with caplog.at_level("WARNING", logger="shelfos"):
        _check_label_settings()
    assert "Ignoring SHELFOS_LABEL_LENGTH_MM" in caplog.text

    caplog.clear()
    monkeypatch.delenv("SHELFOS_LABEL_LENGTH_MM")
    monkeypatch.setattr(config, "LABEL_TAPE", "62")
    with caplog.at_level("WARNING", logger="shelfos"):
        _check_label_settings()
    assert caplog.text == ""  # a working setup says nothing


def _labels(count: int = 3) -> list[LabelData]:
    return [
        LabelData(id=n, name=f"D{n}", path=f"Lab / D{n}", qr_svg="")
        for n in range(1, count + 1)
    ]


def test_printing_writes_a_real_raster_job_to_the_device(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The device is a file here, so the encoder and the write both really run."""
    device = tmp_path / "lp0"
    device.touch()  # a device node exists before anything writes to it

    assert lp.print_labels(_labels(3), device=str(device)).sent == 3
    job = device.read_bytes()
    # A Brother QL job opens by switching to raster mode and ends with
    # print-and-eject; anything else would be a job the printer discards.
    assert job.startswith(b"\x1bia\x01")
    assert job.endswith(b"\x1a")

    one = tmp_path / "lp1"
    one.touch()
    lp.print_labels(_labels(1), device=str(one))
    assert len(one.read_bytes()) < len(job)  # three labels really are three


def test_copies_multiply_the_job(tmp_path) -> None:  # type: ignore[no-untyped-def]
    single = tmp_path / "single"
    double = tmp_path / "double"
    single.touch()
    double.touch()
    lp.print_labels(_labels(1), device=str(single))
    assert lp.print_labels(_labels(1), copies=2, device=str(double)).sent == 2
    assert len(double.read_bytes()) > len(single.read_bytes())


def test_printing_needs_a_configured_device(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    with pytest.raises(ValidationError, match="SHELFOS_LABEL_DEVICE"):
        lp.print_labels(_labels(1))


def test_an_empty_or_oversized_job_is_refused(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "lp0").touch()
    device = str(tmp_path / "lp0")
    with pytest.raises(ValidationError, match="nothing to print"):
        lp.print_labels([], device=device)

    # Far below the 500-label cap on *building* labels: half a roll fed out by
    # one mis-click cannot be undone by reloading the page.
    monkeypatch.setattr(config, "LABEL_MAX_JOB", 2)
    with pytest.raises(ValidationError, match="at most 2 labels"):
        lp.print_labels(_labels(3), device=device)
    with pytest.raises(ValidationError, match="at most 2 labels"):
        lp.print_labels(_labels(2), copies=2, device=device)


def test_a_missing_device_is_a_printer_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Not a ValidationError: the request was fine, the printer was not there."""
    with pytest.raises(PrinterError, match="not there"):
        lp.print_labels(_labels(1), device=str(tmp_path / "gone" / "lp0"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to any file regardless")
def test_an_unwritable_device_names_the_permission_fix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    unwritable = tmp_path / "lp0"
    unwritable.write_bytes(b"")
    unwritable.chmod(0o400)
    with pytest.raises(PrinterError, match="'lp' group"):
        lp.print_labels(_labels(1), device=str(unwritable))


def test_a_busy_printer_is_reported_rather_than_waited_on(
    tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(config, "LABEL_PRINT_TIMEOUT", 0.05)
    (tmp_path / "lp0").touch()
    lp._PRINT_LOCK.acquire()
    try:
        with pytest.raises(PrinterError, match="busy"):
            lp.print_labels(_labels(1), device=str(tmp_path / "lp0"))
    finally:
        lp._PRINT_LOCK.release()
    # The lock is released again for the next job, not leaked by the failure.
    assert lp.print_labels(_labels(1), device=str(tmp_path / "lp0")).sent == 1


def test_a_device_check_is_reported_at_startup(tmp_path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    from app.main import _check_label_settings

    monkeypatch.setattr(config, "LABEL_DEVICE", str(tmp_path / "lp0"))
    with caplog.at_level("WARNING", logger="shelfos"):
        _check_label_settings()
    assert "does not exist" in caplog.text

    caplog.clear()
    device = tmp_path / "lp0"
    device.write_bytes(b"")
    device.chmod(0o400)
    with caplog.at_level("WARNING", logger="shelfos"):
        _check_label_settings()
    assert "not writable" in caplog.text


def test_two_colour_tape_is_recognised_from_the_tape_table() -> None:
    assert not lp.tape_geometry(tape="62").two_color
    assert lp.tape_geometry(tape="62red").two_color


def test_a_two_colour_tape_gets_a_two_colour_job(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """DK-22251 is not an option to support later; it is the tape in the box.

    A QL-800 with black/red tape loaded REFUSES a one-colour job and reports an
    error with no error bits set — which reads like a broken printer rather than
    a mismatched job, and cost an evening to work out on real hardware. The job
    for such a tape has to carry two raster planes and say so in expanded mode.
    """
    monkeypatch.setattr(config, "LABEL_TAPE", "62red")
    device = tmp_path / "lp0"
    device.touch()
    assert lp.print_labels(_labels(1), device=str(device)).sent == 1
    job = device.read_bytes()

    # Expanded mode bit 0 = two-colour printing.
    at = job.find(b"\x1b\x69\x4b")
    assert job[at + 3] & 0x01

    # Raster lines carry a plane selector (black 0x01, red 0x02) instead of the
    # one-colour transfer command.
    assert b"\x77\x01" in job and b"\x77\x02" in job
    assert b"\x67\x00\x5a" not in job

    monkeypatch.setattr(config, "LABEL_TAPE", "62")
    mono = tmp_path / "lp1"
    mono.touch()
    lp.print_labels(_labels(1), device=str(mono))
    # Two planes for the same label: about twice the data.
    assert len(job) > 1.8 * len(mono.read_bytes())


# A real frame, captured from the QL-800 on the bench: 62 mm continuous tape,
# no errors, answering a status question. Keeping the actual bytes means the
# decoder is tested against the printer rather than against my reading of the
# specification.
_IDLE_FRAME = bytes.fromhex(
    "80 20 42 34 38 30 00 00 00 00 3e 0a 00 00 23 00"
    "00 00 00 01 00 00 00 00 00 81 00 00 00 00 00 00".replace(" ", "")
)


def test_status_decodes_a_real_frame() -> None:
    status = lp._decode_status(_IDLE_FRAME)
    assert status is not None
    assert status.media_width_mm == 62
    assert status.media_type == 0x0A  # continuous
    assert status.has_tape
    assert status.errors == ()
    assert status.status_type == lp._STATUS_REPLY


def test_status_ignores_anything_that_is_not_a_frame() -> None:
    assert lp._decode_status(b"") is None
    assert lp._decode_status(b"\x00" * 32) is None  # no 0x80 0x20 mark
    assert lp._decode_status(_IDLE_FRAME[:20]) is None  # truncated


def test_status_names_the_printer_s_own_error_bits() -> None:
    frame = bytearray(_IDLE_FRAME)
    frame[8] = 0x02  # end of media
    frame[9] = 0x10  # cover open
    status = lp._decode_status(bytes(frame))
    assert status is not None
    assert status.errors == ("the tape has run out", "the cover is open")


def test_a_printer_that_reports_trouble_is_not_printed_to() -> None:
    """Better to say what the printer said than to feed a job into a jam."""
    geometry = lp.tape_geometry(tape="62")
    frame = bytearray(_IDLE_FRAME)
    frame[9] = 0x10
    status = lp._decode_status(bytes(frame))
    assert status is not None
    with pytest.raises(PrinterError, match="cover is open"):
        lp._refuse_if_not_ready(status, geometry)

    frame = bytearray(_IDLE_FRAME)
    frame[11] = 0x00  # no media type = nothing loaded
    empty = lp._decode_status(bytes(frame))
    assert empty is not None
    with pytest.raises(PrinterError, match="no tape"):
        lp._refuse_if_not_ready(empty, geometry)


def _frame(**fields: int) -> lp.PrinterStatus:
    """The bench frame with a few bytes changed, decoded."""
    raw = bytearray(_IDLE_FRAME)
    for offset, value in fields.items():
        raw[int(offset[1:])] = value
    status = lp._decode_status(bytes(raw))
    assert status is not None
    return status


def test_the_tape_is_recognised_from_what_the_printer_reports(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Width and continuous-versus-die-cut name the tape; nobody has to."""
    monkeypatch.setattr(config, "LABEL_TAPE", "62")
    assert lp.detect_tape(_frame()) == "62"  # 62 mm continuous, from the bench

    # A die-cut roll reports its length too, which pins the exact die.
    assert lp.detect_tape(_frame(b11=0x0B, b17=29)) == "62x29"
    assert lp.detect_tape(_frame(b10=29, b11=0x0B, b17=90)) == "29x90"
    assert lp.detect_tape(_frame(b10=29)) == "29"

    # Nothing sensible to say about a width no tape has, or an empty printer.
    assert lp.detect_tape(_frame(b10=99)) is None
    assert lp.detect_tape(_frame(b11=0x00)) is None


def test_the_configured_tape_settles_what_the_printer_cannot_say(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A black/red roll is indistinguishable from a plain one in the status
    frame, so the configuration decides the colour — and only the colour."""
    monkeypatch.setattr(config, "LABEL_TAPE", "62red")
    assert lp.detect_tape(_frame()) == "62red"

    # But it cannot override the geometry: 29 mm is not a 62 mm roll.
    assert lp.detect_tape(_frame(b10=29)) == "29"


def test_the_layout_follows_the_printer_not_the_configuration(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The point of asking: a roll swap changes the labels, not the settings."""
    monkeypatch.setattr(config, "LABEL_TAPE", "62")
    assert _frame(b10=29).media_width_mm == 29
    assert lp._geometry_for(_frame(b10=29)).tape == "29"
    # A printer that says nothing leaves the configured tape in charge.
    assert lp._geometry_for(None).tape == "62"


def test_an_unknown_tape_is_refused_rather_than_guessed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "LABEL_TAPE", "62")
    with pytest.raises(ValidationError, match="matches no tape"):
        lp._refuse_if_not_ready(_frame(b10=99), lp.tape_geometry(tape="62"))


def test_a_refusal_with_no_reason_given_names_the_likely_tape_mismatch(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """A QL-800 with black/red tape refuses a one-colour job and says nothing
    else about it. Guessing what it meant cost an evening; the message guesses
    now, naming the tape setting that is almost always the cause."""
    frame = bytearray(_IDLE_FRAME)
    frame[18] = lp._STATUS_ERROR
    refusal = lp._decode_status(bytes(frame))
    monkeypatch.setattr(lp, "_read_status", lambda fd, budget: refusal)

    with pytest.raises(PrinterError, match="62red"):
        lp._await_completion(0, budget=1.0)


def test_a_device_that_cannot_be_asked_still_prints_unconfirmed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A plain file is not a printer: print, and say the job was only sent."""
    device = tmp_path / "lp0"
    device.touch()
    outcome = lp.print_labels(_labels(1), device=str(device))
    assert (outcome.sent, outcome.confirmed) == (1, False)


def test_text_never_runs_off_the_end_of_the_tape() -> None:
    """The one bad label this module could still print quietly.

    Type was shrunk to the box's width and never its height, so on a short
    label a long path wrapped to three lines and the last one printed past the
    cut. Every other impossible case here is refused outright; this one went
    through. The paths are the ones the bulk generator makes easy to produce.
    """
    label = LabelData(
        id=123,
        name="Drawer 03",
        path="Workshop / Wall unit B / Rack A / Shelf 02 / Drawer 03",
        qr_svg="",
    )
    margin = round(config.LABEL_MARGIN_MM * 300 / 25.4)
    for length in (12, 15, 20, 30, 60):
        geometry = lp.tape_geometry(length_mm=length)
        box = _ink_box(lp.render_label(label, geometry))
        assert box is not None
        assert box[3] <= geometry.length_px - margin, f"{length} mm overflows"
        assert box[1] >= margin, f"{length} mm overflows the top"


def test_a_short_label_keeps_the_name_and_the_useful_end_of_the_path() -> None:
    """Fitting to the height drops lines, and drops them from the front."""
    label = LabelData(
        id=123,
        name="Drawer 03",
        path="Workshop / Wall unit B / Rack A / Shelf 02 / Drawer 03",
        qr_svg="",
    )
    lines, height = lp._text_block(label, box_w=318, box_h=94)  # a 12 mm label
    assert height <= 94
    assert [text for text, _, _ in lines][0] == "Drawer 03"
    assert [text for text, _, _ in lines][-1].endswith("Drawer 03")
    # A 30 mm label has room for the whole thing, so nothing is dropped there.
    tall, _ = lp._text_block(label, box_w=318, box_h=306)
    assert len(tall) > len(lines)


def test_a_non_positive_print_timeout_is_refused_not_obeyed(  # type: ignore[no-untyped-def]
    monkeypatch, caplog
) -> None:
    """A negative wait is not a shorter wait but an endless one, on a thread
    that is meant to be handed back — and zero seconds cannot send a job at
    all. Both are plausible typos while tuning, so they are refused by name
    rather than acted on."""
    from app.main import _check_label_settings

    for value in (-1.0, 0.0):
        caplog.clear()
        monkeypatch.setattr(config, "LABEL_PRINT_TIMEOUT", value)
        with caplog.at_level("WARNING", logger="shelfos"):
            _check_label_settings()
        assert "SHELFOS_LABEL_PRINT_TIMEOUT" in caplog.text

        # In a thread, because the failure this guards against is a hang: a
        # regression should fail the test rather than stop the suite.
        outcome: list[str] = []

        def attempt(into: list[str] = outcome) -> None:
            try:
                lp.print_labels(_labels(1), device="/nonexistent/lp0")
            except Exception as error:  # noqa: BLE001 - the type is the assertion
                into.append(type(error).__name__)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), f"{value} waited instead of refusing"
        assert outcome == ["ValidationError"]


def test_zero_status_timeout_turns_the_readback_off(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """Zero is what someone reaches for to mean "do not ask the printer
    anything". It used to be read as a deadline already passed, so the status
    request failed before a byte left and the printer was blamed for it."""
    from app.main import _check_label_settings

    monkeypatch.setattr(config, "LABEL_STATUS_TIMEOUT", 0.0)
    assert lp._readback_budget() == 0
    with caplog.at_level("INFO", logger="shelfos"):
        _check_label_settings()
    assert "not asking the printer anything" in caplog.text

    # /dev/null is a character device that never answers: with the readback off
    # it is written to and reported as sent, immediately and without complaint.
    outcome = lp.print_labels(_labels(1), device="/dev/null")
    assert (outcome.sent, outcome.confirmed) == (1, False)


# --- The whole round trip, against a pty pretending to be a QL ---------------
#
# Everything above tests the pieces: decoders against captured frames, refusals
# against synthetic statuses, jobs against a file. What none of it covered is
# the wiring — that a printer which answers gets asked, believed, and confirmed.
# `confirmed` was asserted False in two places and true in none, so cutting the
# wire entirely (`confirmed = False and ...`) survived the suite.


def test_a_printer_that_answers_is_asked_believed_and_confirmed() -> None:
    """The headline of the feature, end to end and in milliseconds."""
    with FakePrinter([IDLE_FRAME, frame(b18=lp._STATUS_COMPLETED)]) as printer:
        outcome = lp.print_labels(_labels(1), device=printer.path)
        printer.wait_for(b"\x1a")  # the job's tail is still in flight

    assert outcome.confirmed  # the printer said it printed, and was heard
    assert outcome.sent == 1
    assert outcome.tape == "62"  # from the frame, not from configuration
    # A whole raster job really went down the wire after the status question,
    # not just the question itself.
    assert printer.received.startswith(lp._STATUS_REQUEST)
    assert printer.received[len(lp._STATUS_REQUEST) :].startswith(b"\x1b\x69\x61")
    assert printer.received.endswith(b"\x1a")  # print and eject
    assert len(printer.received) > 30_000


def test_a_printer_reporting_a_fault_is_not_printed_to() -> None:
    """The refusal must come BEFORE any tape moves, which the byte count shows:
    only the three-byte status question reached the device."""
    with FakePrinter([frame(b8=0x02)]) as printer:  # end of media
        with pytest.raises(PrinterError, match="tape has run out"):
            lp.print_labels(_labels(1), device=printer.path)

        assert bytes(printer.received) == lp._STATUS_REQUEST


def test_the_tape_the_printer_reports_decides_the_label(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A roll swap changes the labels, not the settings — through the real
    device, not a synthesised status object."""
    monkeypatch.setattr(config, "LABEL_TAPE", "62")
    monkeypatch.setattr(lp, "_tape_cache", None)
    # One frame for the preview's question, one for the print's own, one to
    # confirm — the print never trusts a remembered answer.
    answers = [frame(b10=29), frame(b10=29), frame(b10=29, b18=lp._STATUS_COMPLETED)]
    with FakePrinter(answers) as printer:
        monkeypatch.setattr(config, "LABEL_DEVICE", printer.path)
        assert lp.resolve_geometry().width_px == 306  # a 29 mm roll

        monkeypatch.setattr(lp, "_tape_cache", None)
        outcome = lp.print_labels(_labels(1), device=printer.path)
    assert outcome.tape == "29"


def test_the_tape_is_remembered_briefly_rather_than_asked_every_time(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """A preview per label would otherwise be a status round trip per label,
    each taking the print lock, from any account allowed to look."""
    monkeypatch.setattr(lp, "_tape_cache", None)
    with FakePrinter([frame(b10=29)]) as printer:  # one frame, two questions
        monkeypatch.setattr(config, "LABEL_DEVICE", printer.path)
        assert lp.resolve_geometry().tape == "29"
        asked_once = len(printer.received)
        assert lp.resolve_geometry().tape == "29"  # answered from memory
        assert len(printer.received) == asked_once


def test_a_run_can_be_stopped_part_way_through() -> None:
    """A cabinet started by mistake is hundreds of labels, and a job handed to
    the printer whole cannot be called back — the buffer belongs to the machine.
    So labels go one at a time, and a stop takes effect between them."""
    labels = _labels(12)
    frames = [_IDLE_FRAME] + [frame(b18=lp._STATUS_COMPLETED)] * 40

    # Asked from the fake, so the stop lands between labels rather than after a
    # sleep long enough to be flaky.
    with FakePrinter(frames, on_page=lambda page: page == 2 and lp.request_stop()) as p:
        outcome = lp.print_labels(labels, device=p.path)

    assert outcome.stopped
    assert 0 < outcome.sent < len(labels)  # some came out; most did not
    # And the printer was never handed the rest: the bytes stop where the run did.
    assert len(p.received) < len(labels) * 20_000


def test_a_stop_does_not_leak_into_the_next_run() -> None:
    """Otherwise one cancelled cabinet would quietly cancel the next job too."""
    lp.request_stop()
    with FakePrinter([_IDLE_FRAME, frame(b18=lp._STATUS_COMPLETED)]) as printer:
        outcome = lp.print_labels(_labels(1), device=printer.path)
    assert (outcome.sent, outcome.stopped) == (1, False)


def test_progress_is_readable_while_a_run_is_going_and_clear_after() -> None:
    seen: list[tuple[int, int] | None] = []
    with FakePrinter(
        [_IDLE_FRAME] + [frame(b18=lp._STATUS_COMPLETED)] * 20,
        on_page=lambda _page: seen.append(lp.job_progress()),
    ) as printer:
        lp.print_labels(_labels(3), device=printer.path)

    assert any(p is not None for p in seen)  # something to show a watcher
    assert lp.job_progress() is None  # and nothing left behind afterwards

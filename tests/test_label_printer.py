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

import pytest
import segno
from app import config
from app.services import label_printer as lp
from app.services.errors import PrinterError, ValidationError
from app.services.label_service import LabelData, location_qr_payload
from PIL import Image, ImageChops

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


def test_a_tape_too_narrow_for_a_readable_qr_is_refused() -> None:
    # 12 mm of tape leaves 106 dots across, so the code would print at under
    # 0.25 mm per module — a decorative square, not something a phone reads.
    with pytest.raises(ValidationError, match="too small to scan"):
        lp.render_label(_label(), lp.tape_geometry(tape="12"))


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

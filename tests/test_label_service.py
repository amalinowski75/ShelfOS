"""Tests for label_service: QR payloads and printable label assembly."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.models.component import Component, ComponentType
from app.models.enums import LocationType
from app.services import label_service as lbl
from app.services import location_service as ls
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session


def test_location_qr_payload_is_prefixed_and_minimal() -> None:
    # Terse and all in the QR alphanumeric set (digits + uppercase): a longer or
    # lowercase prefix would force byte mode and a bigger, denser code. The
    # prefix is what will let a scan dispatcher tell a location label from a
    # supplier barcode.
    assert lbl.location_qr_payload(123) == "SL123"


def test_qr_svg_is_an_inline_fragment() -> None:
    svg = lbl.qr_svg("SL1")
    assert svg.startswith("<svg")
    assert "<?xml" not in svg  # embedded in HTML, not a standalone document
    assert 'viewBox="' in svg  # scales by CSS, no fixed pixel size
    assert "width=" not in svg.split(">", 1)[0]


def test_qr_stays_at_the_smallest_full_version_for_realistic_ids() -> None:
    # Version 1 (21×21 modules) up to 18-digit ids — and a full QR, never a
    # Micro QR, which phone cameras often refuse to read.
    import segno

    for location_id in (1, 125, 99_999, int("9" * 18)):
        qr = segno.make(lbl.location_qr_payload(location_id), error="m", micro=False)
        assert qr.version == 1
        assert qr.mode == "alphanumeric"


def _hierarchy(session: Session) -> tuple[int, int, int]:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )
    drawer = ls.create_location(
        session, type=LocationType.DRAWER, name="D1", parent_id=rack.id
    )
    ls.create_location(session, type=LocationType.ROOM, name="Bench")
    assert lab.id and rack.id and drawer.id
    return lab.id, rack.id, drawer.id


def test_build_labels_for_a_subtree(session: Session) -> None:
    lab_id, rack_id, drawer_id = _hierarchy(session)
    labels = lbl.build_labels(session, root=rack_id)
    assert [(label.id, label.detail) for label in labels] == [
        (rack_id, "Lab / Rack A"),
        (drawer_id, "Lab / Rack A / D1"),
    ]
    assert "<svg" in labels[0].qr_svg


def test_build_labels_for_explicit_ids_keeps_their_order(session: Session) -> None:
    lab_id, _rack_id, drawer_id = _hierarchy(session)
    labels = lbl.build_labels(session, ids=[drawer_id, lab_id])
    assert [label.id for label in labels] == [drawer_id, lab_id]
    assert labels[0].detail == "Lab / Rack A / D1"


def test_build_labels_defaults_to_everything_in_tree_order(session: Session) -> None:
    _hierarchy(session)
    labels = lbl.build_labels(session)
    # Pre-order, roots sorted by name: Bench first, then Lab's subtree.
    assert [label.name for label in labels] == ["Bench", "Lab", "Rack A", "D1"]


def test_build_labels_rejects_unknown_locations(session: Session) -> None:
    with pytest.raises(NotFoundError):
        lbl.build_labels(session, root=999)
    with pytest.raises(NotFoundError):
        lbl.build_labels(session, ids=[999])


def test_build_labels_dedupes_ids_and_caps_the_request(session: Session) -> None:
    lab_id, _rack_id, _drawer_id = _hierarchy(session)
    # Repeating an id costs one label, not one per repetition.
    labels = lbl.build_labels(session, ids=[lab_id, lab_id, lab_id])
    assert [label.id for label in labels] == [lab_id]

    # The cap fires before any lookup or QR work — same limit as bulk create.
    too_many = list(range(1, ls._MAX_BULK_NODES + 2))
    with pytest.raises(ValidationError, match="at most"):
        lbl.build_labels(session, ids=too_many)


def _component(session: Session, **fields: object) -> int:
    ctype = ComponentType(name="MCU")
    session.add(ctype)
    session.commit()
    component = Component(type_id=ctype.id, **fields)  # type: ignore[arg-type]
    session.add(component)
    session.commit()
    assert component.id
    return component.id


def test_component_qr_payload_is_prefixed_and_distinct_from_a_location() -> None:
    assert lbl.component_qr_payload(123) == "SC123"
    # The two prefixes are what a scan dispatches on, so they must not collide.
    assert lbl.component_qr_payload(1) != lbl.location_qr_payload(1)


def test_component_qr_stays_at_the_smallest_full_version() -> None:
    import segno

    for component_id in (1, 125, 99_999, int("9" * 18)):
        qr = segno.make(lbl.component_qr_payload(component_id), error="m", micro=False)
        assert qr.version == 1
        assert qr.mode == "alphanumeric"


def test_scanned_component_id_reads_our_own_label_and_nothing_else() -> None:
    assert lbl.scanned_component_id("SC42") == 42
    assert lbl.scanned_component_id("  sc42 ") == 42  # typed by hand, and padded
    # A shelf label, a supplier's part number and a URL are all somebody else's.
    assert lbl.scanned_component_id("SL42") is None
    assert lbl.scanned_component_id("SC42A") is None
    assert lbl.scanned_component_id("STM32F103C8T6") is None
    assert lbl.scanned_component_id("https://www.tme.eu/SC42") is None
    # Past SQLite's rowid range: naming nothing, so it is not read as ours —
    # int() would parse it happily and the query would then raise mid-flight.
    assert lbl.scanned_component_id(f"SC{2**63}") is None


def test_component_label_leads_with_the_part_number(session: Session) -> None:
    component_id = _component(
        session,
        mpn="STM32F103C8T6",
        manufacturer="STMicroelectronics",
        notes="ARM Cortex-M3 MCU, 64 kB Flash",
    )
    (label,) = lbl.build_component_labels(session, [component_id])
    assert label.name == "STM32F103C8T6"
    # The maker keeps a line of its own; the description follows it.
    assert label.detail == "STMicroelectronics\nARM Cortex-M3 MCU, 64 kB Flash"
    assert label.qr_payload == f"SC{component_id}"
    # Prose, not a path: broken at spaces, and cut from the end when it will not
    # fit — the opposite of a location's answers on both counts.
    assert (label.separator, label.trim) == (" ", "tail")
    # These print to tape, so no SVG is rendered for a page that does not exist.
    assert label.qr_svg == ""


def test_component_label_leaves_out_what_the_component_has_not_got(
    session: Session,
) -> None:
    # No maker, no description: no blank line where they would have been, and a
    # part with no number is still identifiable.
    component_id = _component(session)
    (label,) = lbl.build_component_labels(session, [component_id])
    assert label.name == f"Component #{component_id}"
    assert label.detail == ""

    maker_only = _component(session, mpn="X", manufacturer="Acme")
    (label,) = lbl.build_component_labels(session, [maker_only])
    assert label.detail == "Acme"


def test_component_label_collapses_and_caps_every_free_text_field(
    session: Session,
) -> None:
    """None of the three fields carries a max_length, and all three are drawn.

    The fitter measures the string it is given once per type size it tries, and
    a line with nothing to wrap at is then ellipsised one character at a time —
    so an uncapped field is real work for one GET of a preview, whichever field
    it is.
    """
    component_id = _component(
        session,
        mpn="MPN" + "x" * 500,
        manufacturer="Maker " + "y" * 500,
        notes="pasted\nfrom  a   datasheet " + "very long " * 40,
    )
    (label,) = lbl.build_component_labels(session, [component_id])
    maker, description = label.detail.split("\n")

    # The breaks and runs the text was pasted with are not the label's breaks.
    assert description.startswith("pasted from a datasheet very long")
    for field in (label.name, maker, description):
        # Capped, and it says so, so a shortened value does not read as whole.
        assert len(field) <= lbl._MAX_TEXT_CHARS + 1
        assert field.endswith("…")


def test_a_deleted_component_gets_no_label(session: Session) -> None:
    """Scanning that very label answers "out of date", so printing it is wrong.

    ``components_by_id`` deliberately includes retired parts — its callers are
    the ones that have to SHOW them — so this has to say so itself, or the API
    would lay tape for a label the rest of the system has already disowned.
    """
    component_id = _component(session, mpn="OLD-1")
    component = session.get(Component, component_id)
    assert component is not None
    component.deleted_at = datetime(2026, 9, 1, tzinfo=UTC)
    session.add(component)
    session.commit()

    with pytest.raises(ValidationError, match="nothing to label"):
        lbl.build_component_labels(session, [component_id])


def test_component_labels_keep_their_order_dedupe_and_refuse_the_unknown(
    session: Session,
) -> None:
    first = _component(session, mpn="A")
    second = _component(session, mpn="B")
    labels = lbl.build_component_labels(session, [second, first, second])
    assert [label.name for label in labels] == ["B", "A"]

    with pytest.raises(NotFoundError):
        lbl.build_component_labels(session, [9999])

    with pytest.raises(ValidationError, match="at most"):
        lbl.build_component_labels(session, list(range(1, ls._MAX_BULK_NODES + 2)))

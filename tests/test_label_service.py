"""Tests for label_service: QR payloads and printable label assembly."""

from __future__ import annotations

import pytest
from app.models.enums import LocationType
from app.services import label_service as lbl
from app.services import location_service as ls
from app.services.errors import NotFoundError
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
    assert [(label.id, label.path) for label in labels] == [
        (rack_id, "Lab / Rack A"),
        (drawer_id, "Lab / Rack A / D1"),
    ]
    assert "<svg" in labels[0].qr_svg


def test_build_labels_for_explicit_ids_keeps_their_order(session: Session) -> None:
    lab_id, _rack_id, drawer_id = _hierarchy(session)
    labels = lbl.build_labels(session, ids=[drawer_id, lab_id])
    assert [label.id for label in labels] == [drawer_id, lab_id]
    assert labels[0].path == "Lab / Rack A / D1"


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

"""Service tests for KiCad BOM import + availability report (spec §21/§22)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import config
from app.models.enums import LocationType, ParameterDataType
from app.services import bom_service as bs
from app.services import component_service as cs
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import ValidationError
from sqlmodel import Session

_FIXTURE = (Path(__file__).parent / "fixtures" / "kicad_bom.csv").read_bytes()


@pytest.fixture
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the attachment store (the saved CSV) at a throwaway directory."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


# --- parsing ---------------------------------------------------------------


def test_parse_real_kicad_layout() -> None:
    lines = bs.parse_bom(_FIXTURE, filename="kicad_bom.csv")
    by_ref = {line.references.split(",")[0]: line for line in lines}

    r = by_ref["R3"]  # grouped refs kept, Qty from column, category + MPN
    assert r.references == "R3,R10,R26"
    assert r.category == "resistor" and r.quantity == 3 and r.mpn == "RES-1K-0402"

    c = by_ref["C1"]  # value with /voltage; leading-space manufacturer stripped
    assert c.category == "capacitor" and c.manufacturer == "MURATA"

    assert by_ref["R4"].mpn is None  # blank MPN cell → None
    assert by_ref["Q9"].category == "transistor"  # Q prefix
    assert by_ref["SP1"].category is None  # unknown prefix → no category


def test_clean_value_handles_suffixes_and_part_numbers() -> None:
    assert bs.clean_value("1k 1%") == 1000.0
    assert bs.clean_value("10uF/50V") == 1e-5
    assert bs.clean_value("22p/50V") == 22e-12
    assert bs.clean_value("0R") == 0.0
    assert bs.clean_value("AO3400A") is None  # a part number, not a scalar
    assert bs.clean_value(None) is None


def test_parse_rejects_empty_columnless_and_header_only() -> None:
    with pytest.raises(ValidationError):
        bs.parse_bom(b"   ", filename="x.csv")
    with pytest.raises(ValidationError):
        bs.parse_bom(b"Foo,Bar\n1,2\n", filename="x.csv")
    with pytest.raises(ValidationError):
        bs.parse_bom(b"Reference,Value\n", filename="x.csv")  # header, no rows


def test_quantity_derived_when_no_qty_column() -> None:
    lines = bs.parse_bom(b'Reference,Value\n"R1,R2,R3",1k\n', filename="x.csv")
    assert lines[0].quantity == 3


def test_parse_accepts_a_semicolon_delimiter() -> None:
    lines = bs.parse_bom(b"Reference;Qty;Value\nR1;1;10k\n", filename="x.csv")
    assert lines[0].value == "10k" and lines[0].quantity == 1


def test_unparseable_qty_falls_back_to_the_designator_count() -> None:
    # "1e400"/"inf" would overflow int(float(...)) — must not crash; count refdes.
    lines = bs.parse_bom(b'Reference,Qty,Value\n"R1,R2",1e400,10k\n', filename="x.csv")
    assert lines[0].quantity == 2


# --- report ----------------------------------------------------------------


def _inventory(session: Session):  # type: ignore[no-untyped-def]
    """A resistor type + a drawer; returns a factory for stocked resistors."""
    ctype = cs.create_type(session, "resistor")
    rdef = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
        unit="ohm",
    )
    drawer = ls.create_location(session, type=LocationType.DRAWER, name="D1")

    def resistor(mpn: str, ohms: float, stock: int) -> None:
        component = cs.create_component_with_values(
            session, ctype.id, mpn=mpn, values=[(rdef.id, ohms)]
        )
        if stock:
            ss.add_stock(
                session,
                component_id=component.id,
                location_id=drawer.id,
                quantity=stock,
                user_id=1,
            )

    return resistor


def test_report_matches_by_mpn_and_suggests_substitutes(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 50)  # exact 1k, in stock
    resistor("RES-1K1", 1050, 20)  # 1.05k — within the ±10% band
    resistor("RES-4K7", 4700, 0)  # 4.7k but out of stock

    data = (
        b"Reference,Qty,Value,MPN\n"
        b"R1,10,1k,RES-1K\n"  # MPN match, enough stock
        b"R2,5,1k,\n"  # no MPN → substitutes by value
        b"R3,5,4.7k,\n"  # value exists but only out-of-stock → no substitute
    )
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    report = bs.build_bom_report(session, bom.id)
    lines = {line["references"]: line for line in report["lines"]}

    assert lines["R1"]["status"] == "ok"
    assert lines["R1"]["substitutes"] == []  # a satisfied line gets no suggestions

    subs = lines["R2"]["substitutes"]
    assert [s["mpn"] for s in subs] == ["RES-1K", "RES-1K1"]  # exact first, then near
    assert subs[0]["exact"] is True and subs[1]["exact"] is False

    assert lines["R3"]["status"] == "no_mpn"
    assert lines["R3"]["substitutes"] == []  # the only 4.7k is out of stock


def test_buildable_is_the_limiting_line(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 50)
    resistor("RES-2K", 2000, 12)
    data = (
        b"Reference,Qty,Value,MPN\n"
        b"R1,10,1k,RES-1K\n"  # 50 // 10 = 5
        b"R2,4,2k,RES-2K\n"  # 12 // 4 = 3  → limits the board
    )
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    summary = bs.build_bom_report(session, bom.id)["summary"]
    assert summary["ok"] == 2 and summary["buildable"] == 3


def test_buildable_is_zero_when_a_line_is_unavailable(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 50)
    data = (
        b"Reference,Qty,Value,MPN\n"
        b"R1,10,1k,RES-1K\n"  # plenty
        b"R2,5,2k,RES-NOPE\n"  # MPN not in inventory → missing
    )
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    summary = bs.build_bom_report(session, bom.id)["summary"]
    assert summary["missing"] == 1
    assert summary["buildable"] == 0  # a missing line caps true buildability


def test_mpn_match_is_case_insensitive(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    resistor = _inventory(session)
    resistor("ABC-1K", 1000, 50)
    data = b"Reference,Qty,Value,MPN\nR1,10,1k,abc-1k\n"  # lower-case in the BOM
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    lines = bs.build_bom_report(session, bom.id)["lines"]
    assert lines[0]["status"] == "ok"


def test_report_footprint_strips_the_library_prefix(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    data = (
        b"Reference,Qty,Value,Footprint\n"
        b"R1,1,1k,Resistor_SMD:R_0402_1005Metric\n"
        b"SP1,1,SPK,footprints:spk\n"
        b"J1,1,CONN,NoColonFootprint\n"
    )
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    lines = bs.build_bom_report(session, bom.id)["lines"]
    assert lines[0]["footprint"] == "R_0402_1005Metric"  # library prefix dropped
    assert lines[1]["footprint"] == "spk"
    assert lines[2]["footprint"] == "NoColonFootprint"  # kept as-is when no ":"


def test_create_bom_is_undone_when_the_attachment_fails(
    session: Session, store, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from app.services import attachment_service as ats

    def boom(*_a: object, **_k: object) -> object:
        raise ValidationError("attachment rejected")

    monkeypatch.setattr(ats, "create_attachment", boom)
    with pytest.raises(ValidationError):
        bs.create_bom(session, name="b", filename="b.csv", data=_FIXTURE, user_id=1)
    assert bs.list_boms(session) == []  # no orphan bom left behind


def test_substitutes_exclude_soft_deleted_components(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    ctype = cs.create_type(session, "resistor")
    rdef = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="ohm",
    )
    drawer = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    comp = cs.create_component_with_values(
        session, ctype.id, mpn="RES-1K", values=[(rdef.id, 1000)]
    )
    ss.add_stock(
        session, component_id=comp.id, location_id=drawer.id, quantity=50, user_id=1
    )
    comp.deleted_at = datetime.now(UTC)  # soft-delete it
    session.add(comp)
    session.commit()

    data = b"Reference,Qty,Value,MPN\nR1,5,1k,\n"  # no MPN → wants substitutes
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    lines = bs.build_bom_report(session, bom.id)["lines"]
    assert lines[0]["substitutes"] == []  # the only 1k is soft-deleted


def test_create_bom_stores_the_original_csv_as_an_attachment(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    from app.services import attachment_service as ats

    bom = bs.create_bom(
        session, name="hiduart", filename="hiduart.csv", data=_FIXTURE, user_id=1
    )
    attachments = ats.list_attachments(session, entity_type="bom", entity_id=bom.id)
    assert len(attachments) == 1 and attachments[0].filename == "hiduart.csv"


def test_delete_bom_removes_lines_and_attachment(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    from app.models.attachment import Attachment
    from sqlmodel import select

    bom = bs.create_bom(
        session, name="b", filename="b.csv", data=_FIXTURE, user_id=1
    )
    bom_id = bom.id
    assert bs.get_bom_lines(session, bom_id)  # lines exist

    bs.delete_bom(session, bom_id)

    assert bs.get_bom_lines(session, bom_id) == []
    # The stored CSV attachment row is gone too (query directly — the bom entity
    # no longer exists, so list_attachments can't be used).
    remaining = session.exec(
        select(Attachment)
        .where(Attachment.entity_type == "bom")
        .where(Attachment.entity_id == bom_id)
    ).all()
    assert remaining == []

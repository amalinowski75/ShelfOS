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
from app.services.errors import NotFoundError, ValidationError
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


# --- reimport (re-parse the stored CSV) ------------------------------------


def test_reimport_reparses_the_stored_csv(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Lines are rebuilt from the stored file, so hand-edited rows are refreshed."""
    from app.models.bom import BomLine

    data = b"Reference,Qty,Value,MPN\nR1,10,1k,RES-1K\n"
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    line = bs.get_bom_lines(session, bom.id)[0]
    original_id = line.id
    line.mpn = "STALE"  # simulate a line that drifted from the file
    session.add(line)
    session.commit()

    bs.reimport_bom(session, bom.id)

    lines = bs.get_bom_lines(session, bom.id)
    assert [ln.mpn for ln in lines] == ["RES-1K"]  # back in step with the CSV
    assert lines[0].id != original_id  # replaced, not patched
    assert session.get(BomLine, original_id) is None  # and the old row is gone


def test_reimport_without_a_stored_csv_is_refused(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    from app.services import attachment_service as ats

    bom = bs.create_bom(
        session, name="b", filename="b.csv", data=_FIXTURE, user_id=1
    )
    ats.delete_attachments_for(session, entity_type="bom", entity_id=bom.id)

    with pytest.raises(ValidationError):
        bs.reimport_bom(session, bom.id)
    assert bs.get_bom_lines(session, bom.id)  # the lines survive the refusal


def test_reimport_uses_the_boms_own_csv_not_whatever_is_attached(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """A stray attachment must not become the source the lines are rebuilt from."""
    from app.models.enums import AttachmentKind
    from app.services import attachment_service as ats

    data = b"Reference,Qty,Value,MPN\nR1,1,1k,RES-1K\n"
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    # Someone attaches another CSV through the attachments API and removes the
    # original — the BOM's own file is gone, but an attachment remains.
    ats.create_attachment(
        session,
        entity_type="bom",
        entity_id=bom.id,
        kind=AttachmentKind.OTHER,
        filename="notes.csv",
        data=b"Reference,Qty,Value,MPN\nR9,1,2k,OTHER-PART\n",
    )
    original = next(
        a
        for a in ats.list_attachments(session, entity_type="bom", entity_id=bom.id)
        if a.filename == "b.csv"
    )
    ats.delete_attachment(session, original.id)

    # Refused, rather than silently rebuilding the BOM from a file that was never
    # its own — a successful parse can't tell the wrong file from the right one.
    with pytest.raises(ValidationError):
        bs.reimport_bom(session, bom.id)
    assert [ln.mpn for ln in bs.get_bom_lines(session, bom.id)] == ["RES-1K"]


def test_reimport_keeps_the_lines_when_the_csv_no_longer_parses(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Parse first, delete second — a broken file must not empty the BOM."""
    from app.services import attachment_service as ats

    bom = bs.create_bom(
        session, name="b", filename="b.csv", data=_FIXTURE, user_id=1
    )
    before = [ln.references for ln in bs.get_bom_lines(session, bom.id)]
    attachment = ats.list_attachments(session, entity_type="bom", entity_id=bom.id)[0]
    ats.stored_file_path(attachment).write_bytes(b"nothing,useful\n1,2\n")

    with pytest.raises(ValidationError):
        bs.reimport_bom(session, bom.id)
    assert [ln.references for ln in bs.get_bom_lines(session, bom.id)] == before


# --- assigning a component to a line ---------------------------------------


def _assignable(session: Session):  # type: ignore[no-untyped-def]
    """A one-line BOM (no MPN), a stocked component to assign, and the factory."""
    resistor = _inventory(session)
    resistor("SOME-OTHER-PART", 1000, 40)
    other = cs.find_components_by_mpn(session, "SOME-OTHER-PART")[0]
    data = b"Reference,Qty,Value,MPN\nR1,10,1k,\n"  # no MPN at all
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    return bom, bs.get_bom_lines(session, bom.id)[0], other, resistor


def test_assignment_stands_in_for_the_mpn_lookup(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    bom, line, other, resistor = _assignable(session)
    # Without an assignment the line has nothing to match on.
    before = bs.build_bom_report(session, bom.id)["lines"][0]
    assert before["status"] == "no_mpn" and before["stock"] == 0

    bs.assign_component(
        session, bom.id, line.id, component_id=other.id, user_id=1
    )

    after = bs.build_bom_report(session, bom.id)["lines"][0]
    assert after["status"] == "ok"  # 40 in stock covers the 10 it needs
    assert after["stock"] == 40  # …read from the assigned component
    assert after["assigned"]["component_id"] == other.id
    assert after["assigned"]["mpn"] == "SOME-OTHER-PART"
    assert after["substitutes"] == []  # the question is already answered


def test_assigning_again_replaces_the_choice(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    bom, line, other, resistor = _assignable(session)
    resistor("SECOND-CHOICE", 2000, 7)
    second = cs.find_components_by_mpn(session, "SECOND-CHOICE")[0]

    bs.assign_component(session, bom.id, line.id, component_id=other.id, user_id=1)
    bs.assign_component(session, bom.id, line.id, component_id=second.id, user_id=1)

    assert len(bs.list_assignments(session, bom.id)) == 1  # one per line, not two
    line_report = bs.build_bom_report(session, bom.id)["lines"][0]
    assert line_report["assigned"]["component_id"] == second.id
    assert line_report["stock"] == 7


def test_assignment_overrides_even_a_line_that_already_matches(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Unconditional by design: what we build from isn't always what the CSV says."""
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 5)
    resistor("RES-1K-ALT", 1000, 500)
    alt = cs.find_components_by_mpn(session, "RES-1K-ALT")[0]
    data = b"Reference,Qty,Value,MPN\nR1,10,1k,RES-1K\n"
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    line = bs.get_bom_lines(session, bom.id)[0]

    bs.assign_component(session, bom.id, line.id, component_id=alt.id, user_id=1)

    report = bs.build_bom_report(session, bom.id)["lines"][0]
    assert report["stock"] == 500  # the alternative's stock, not RES-1K's 5
    assert report["mpn"] == "RES-1K"  # the CSV's own MPN is left alone


def test_an_assigned_line_stops_being_offered_substitutes(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Substitutes answer "what else could go here?" — already answered by hand."""
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 0)  # the 1k the line wants, but out of stock
    resistor("RES-1K1", 1050, 30)  # a near-value alternative that IS stocked
    alt = cs.find_components_by_mpn(session, "RES-1K1")[0]
    data = b"Reference,Qty,Value,MPN\nR1,10,1k,\n"
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    line = bs.get_bom_lines(session, bom.id)[0]
    assert bs.build_bom_report(session, bom.id)["lines"][0]["substitutes"]  # offered

    bs.assign_component(session, bom.id, line.id, component_id=alt.id, user_id=1)
    assert bs.build_bom_report(session, bom.id)["lines"][0]["substitutes"] == []


def test_substitutes_stay_suppressed_when_the_assigned_part_is_retired(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """The line reports `missing`, but the choice stands until someone changes it.

    This is the case where suppression is least obviously right, so pin it: the way
    out is Change/Remove on the row, not a suggestion the assignment overrode.
    """
    resistor = _inventory(session)
    resistor("RES-1K1", 1050, 30)  # a stocked near-value that would be suggested
    resistor("PICKED", 1000, 0)  # what gets assigned, and then retired
    picked = cs.find_components_by_mpn(session, "PICKED")[0]
    data = b"Reference,Qty,Value,MPN\nR1,10,1k,\n"
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)
    line = bs.get_bom_lines(session, bom.id)[0]

    bs.assign_component(session, bom.id, line.id, component_id=picked.id, user_id=1)
    cs.soft_delete_component(session, picked.id, user_id=1)

    report = bs.build_bom_report(session, bom.id)["lines"][0]
    assert report["status"] == "missing" and report["assigned"]["deleted"] is True
    assert report["substitutes"] == []


def test_unassign_returns_the_line_to_mpn_matching(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    bom, line, other, resistor = _assignable(session)
    bs.assign_component(session, bom.id, line.id, component_id=other.id, user_id=1)
    bs.unassign_component(session, bom.id, line.id)

    report = bs.build_bom_report(session, bom.id)["lines"][0]
    assert report["assigned"] is None and report["status"] == "no_mpn"
    with pytest.raises(NotFoundError):  # nothing left to remove
        bs.unassign_component(session, bom.id, line.id)


def test_a_line_from_another_bom_cannot_be_assigned(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    bom, line, other, resistor = _assignable(session)
    elsewhere = bs.create_bom(
        session, name="other", filename="o.csv", data=_FIXTURE, user_id=1
    )
    with pytest.raises(NotFoundError):
        bs.assign_component(
            session, elsewhere.id, line.id, component_id=other.id, user_id=1
        )


def test_a_component_taken_out_of_use_cannot_be_assigned(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    bom, line, other, resistor = _assignable(session)
    resistor("RETIRED", 3300, 0)  # never stocked, so it can be taken out of use
    retired = cs.find_components_by_mpn(session, "RETIRED")[0]
    cs.soft_delete_component(session, retired.id, user_id=1)

    with pytest.raises(ValidationError):
        bs.assign_component(
            session, bom.id, line.id, component_id=retired.id, user_id=1
        )


def test_a_component_retired_after_assignment_still_shows_but_holds_no_stock(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """The assignment stays visible so it can be seen and changed, not silently lost."""
    bom, line, other, resistor = _assignable(session)
    bs.assign_component(session, bom.id, line.id, component_id=other.id, user_id=1)
    # Taking a part out of use means emptying the drawer first (the service refuses
    # while it still holds stock), which is the real order of events.
    held = ss.list_component_locations(session, other.id)[0]
    ss.remove_stock(
        session,
        component_id=other.id,
        location_id=held.location_id,
        quantity=held.quantity,
        user_id=1,
    )
    cs.soft_delete_component(session, other.id, user_id=1)

    report = bs.build_bom_report(session, bom.id)["lines"][0]
    assert report["assigned"]["component_id"] == other.id
    assert report["assigned"]["deleted"] is True
    assert report["status"] == "missing" and report["stock"] == 0


def test_assignments_survive_a_reimport_that_keeps_the_designators(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Keyed by designator group, so re-parsing the same file leaves them alone."""
    bom, line, other, resistor = _assignable(session)
    bs.assign_component(session, bom.id, line.id, component_id=other.id, user_id=1)

    bs.reimport_bom(session, bom.id)  # deletes and recreates every line

    report = bs.build_bom_report(session, bom.id)["lines"][0]
    assert report["assigned"]["component_id"] == other.id


def test_a_reimport_drops_assignments_whose_designators_are_gone(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the row that could remove them no longer exists."""
    from app.services import attachment_service as ats

    bom, line, other, resistor = _assignable(session)
    bs.assign_component(session, bom.id, line.id, component_id=other.id, user_id=1)
    attachment = ats.list_attachments(session, entity_type="bom", entity_id=bom.id)[0]
    ats.stored_file_path(attachment).write_bytes(
        b"Reference,Qty,Value,MPN\nR7,10,1k,\n"  # R1 is gone; R7 is new
    )

    bs.reimport_bom(session, bom.id)

    assert bs.list_assignments(session, bom.id) == []
    assert bs.build_bom_report(session, bom.id)["lines"][0]["assigned"] is None


def test_deleting_a_bom_takes_its_assignments(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    from app.models.bom import BomLineAssignment
    from sqlmodel import select

    bom, line, other, resistor = _assignable(session)
    bs.assign_component(session, bom.id, line.id, component_id=other.id, user_id=1)
    bom_id = bom.id

    bs.delete_bom(session, bom_id)

    remaining = session.exec(
        select(BomLineAssignment).where(BomLineAssignment.bom_id == bom_id)
    ).all()
    assert remaining == []


# --- building several boards -----------------------------------------------


def test_boards_multiply_what_each_line_needs(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 50)
    data = b"Reference,Qty,Value,MPN\nR1,10,1k,RES-1K\n"
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)

    one = bs.build_bom_report(session, bom.id)["lines"][0]
    assert one["status"] == "ok" and one["total_quantity"] == 10

    # 6 boards need 60 of a part we hold 50 of — enough for 5 boards, not 6.
    six = bs.build_bom_report(session, bom.id, boards=6)["lines"][0]
    assert six["total_quantity"] == 60
    assert six["status"] == "short"
    assert six["boards_possible"] == 5  # measured per board, not per run
    assert six["quantity"] == 10  # the per-board figure is untouched


def test_boards_do_not_change_how_many_are_buildable(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    """Asking for more boards can't change what the stock on hand covers."""
    resistor = _inventory(session)
    resistor("RES-1K", 1000, 50)
    resistor("RES-2K", 2000, 12)
    data = (
        b"Reference,Qty,Value,MPN\n"
        b"R1,10,1k,RES-1K\n"  # 50 // 10 = 5
        b"R2,4,2k,RES-2K\n"  # 12 //  4 = 3 → the limiting line
    )
    bom = bs.create_bom(session, name="b", filename="b.csv", data=data, user_id=1)

    for boards in (1, 3, 20):
        summary = bs.build_bom_report(session, bom.id, boards=boards)["summary"]
        assert summary["buildable"] == 3
        assert summary["boards"] == boards  # echoed for the UI

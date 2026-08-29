"""Manufacturer aliases: one canonical spelling, many seen ones.

The problem these solve is that a component's identity is (MPN, manufacturer) and
every source spells the maker differently — so "MICROCHIP" and "Microchip
Technology" used to be two components.
"""

from __future__ import annotations

import pytest
from app.services import component_service as cs
from app.services import manufacturer_service as ms
from app.services.errors import ValidationError
from app.services.shops.base import manufacturer_matches
from sqlmodel import Session


def _part(session: Session, *, mpn: str, manufacturer: str | None):  # type: ignore[no-untyped-def]
    ctype = cs.create_type(session, f"type-{mpn}")
    return cs.create_component_with_values(
        session, ctype.id, mpn=mpn, manufacturer=manufacturer, values=[]
    )


def test_an_unknown_name_is_kept_exactly_as_it_came(session: Session) -> None:
    # Nothing is inferred: without an alias the name stands, whatever it looks like.
    assert ms.canonical_name(session, "Microchip Technology") == "Microchip Technology"
    assert ms.canonical_name(session, "MICROCHIP") == "MICROCHIP"
    assert ms.canonical_name(session, "  ") is None


def test_an_alias_resolves_however_it_is_spelled(session: Session) -> None:
    ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    for spelling in ("MICROCHIP", "microchip", " Microchip ", "micro-chip"):
        assert ms.canonical_name(session, spelling) == "Microchip Technology"


def test_a_spelling_that_only_differs_in_case_records_nothing(
    session: Session,
) -> None:
    # "ONSEMI" against "onsemi" is a difference the lookup already handles, so a row
    # for it would be noise in a table the user reads.
    assert ms.record_alias(session, alias="onsemi", canonical="ONSEMI") is None
    assert ms.list_aliases(session) == []


def test_a_blank_incoming_name_records_nothing(session: Session) -> None:
    # A Farnell invoice prints no manufacturer column at all. Picking an existing
    # part there is a legitimate answer; there is just no spelling to remember.
    assert ms.record_alias(session, alias=None, canonical="ONSEMI") is None
    assert ms.record_alias(session, alias="   ", canonical="ONSEMI") is None
    assert ms.list_aliases(session) == []


def test_an_alias_with_nothing_to_point_at_is_refused(session: Session) -> None:
    with pytest.raises(ValidationError, match="canonical"):
        ms.record_alias(session, alias="MICROCHIP", canonical="  ")


def test_a_second_answer_repoints_the_alias_rather_than_duplicating_it(
    session: Session,
) -> None:
    ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    ms.record_alias(session, alias="microchip", canonical="Microchip Technology Inc.")
    rows = ms.list_aliases(session)
    assert len(rows) == 1  # one row per spelling, not one per answer
    assert rows[0].canonical == "Microchip Technology Inc."  # the newest answer wins


def test_a_deleted_alias_stops_resolving(session: Session) -> None:
    row = ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    assert row is not None
    ms.delete_alias(session, row.id)  # type: ignore[arg-type]
    assert ms.canonical_name(session, "MICROCHIP") == "MICROCHIP"


# --- what the alias changes for a component ---------------------------------


def test_a_component_is_stored_under_the_canonical_name(session: Session) -> None:
    # Resolved on the way IN, so every listing, filter and export agrees without
    # consulting the alias table.
    ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    part = _part(session, mpn="MCP2200", manufacturer="MICROCHIP")
    assert part.manufacturer == "Microchip Technology"


def test_the_same_part_under_an_alias_is_found_as_a_duplicate(
    session: Session,
) -> None:
    _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    # Before the alias the two spellings are two different parts…
    assert (
        cs.find_duplicate_component(session, mpn="MCP2200", manufacturer="MICROCHIP")
        is None
    )
    ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    hit = cs.find_duplicate_component(
        session, mpn="MCP2200", manufacturer="MICROCHIP"
    )
    assert hit is not None
    assert hit.manufacturer == "Microchip Technology"


def test_two_makers_sharing_an_mpn_are_still_two_parts(session: Session) -> None:
    # The reason nothing is inferred: an MPN is not unique across manufacturers.
    _part(session, mpn="5120", manufacturer="Keystone Electronics")
    assert (
        cs.find_duplicate_component(session, mpn="5120", manufacturer="ABB") is None
    )


# --- the candidates the user is asked about ----------------------------------


def test_conflicts_are_the_same_mpn_under_another_name(session: Session) -> None:
    existing = _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    found = cs.find_manufacturer_conflicts(
        session, mpn="MCP2200", manufacturer="MICROCHIP"
    )
    assert [c.id for c in found] == [existing.id]


def test_an_exact_match_is_not_a_conflict(session: Session) -> None:
    # It is the part, not a candidate — asking about it would be noise.
    _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    assert (
        cs.find_manufacturer_conflicts(
            session, mpn="MCP2200", manufacturer="Microchip Technology"
        )
        == []
    )


def test_a_known_alias_settles_it_without_asking(session: Session) -> None:
    # The whole point of recording one: the question is asked once per spelling.
    _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    assert (
        cs.find_manufacturer_conflicts(
            session, mpn="MCP2200", manufacturer="MICROCHIP"
        )
        == []
    )


def test_a_blank_incoming_manufacturer_still_asks(session: Session) -> None:
    # A Farnell invoice prints no maker at all, so today every one of its lines
    # whose MPN already exists quietly becomes a second component.
    existing = _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    found = cs.find_manufacturer_conflicts(session, mpn="MCP2200", manufacturer=None)
    assert [c.id for c in found] == [existing.id]


def test_an_unknown_mpn_asks_nothing(session: Session) -> None:
    _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    assert (
        cs.find_manufacturer_conflicts(session, mpn="NX3P1108", manufacturer="NXP")
        == []
    )
    assert cs.find_manufacturer_conflicts(session, mpn=None, manufacturer="NXP") == []


def test_the_tolerant_comparator_is_not_used_for_identity(session: Session) -> None:
    """Why an alias is required rather than inferred.

    ``manufacturer_matches`` would resolve the reported case for free — and would
    also fuse three real companies, because "micro" is a long-enough prefix of
    "microchip". A missed match costs a duplicate; a false one silently makes two
    different parts into one.
    """
    assert manufacturer_matches("Micro Crystal", "Microchip Technology")
    _part(session, mpn="X1", manufacturer="Microchip Technology")
    assert (
        cs.find_duplicate_component(session, mpn="X1", manufacturer="Micro Crystal")
        is None
    )

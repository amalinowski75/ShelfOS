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


def test_a_chain_is_collapsed_when_the_second_link_is_recorded(
    session: Session,
) -> None:
    """Recording B -> C must also move A -> B on to C.

    The route through the UI is real: a part is filed under "Texas Instr", an
    import of "TI" points an alias at it, and later that part is corrected to
    "Texas Instruments". A one-pass lookup over un-collapsed rows would answer
    "Texas Instr" — a spelling nothing is filed under any more — and the next
    import would quietly start a third variant.
    """
    ms.record_alias(session, alias="TI", canonical="Texas Instr")
    ms.record_alias(session, alias="Texas Instr", canonical="Texas Instruments")
    assert ms.canonical_name(session, "TI") == "Texas Instruments"
    assert {row.canonical for row in ms.list_aliases(session)} == {"Texas Instruments"}


def test_a_chain_is_collapsed_when_the_target_is_already_an_alias(
    session: Session,
) -> None:
    # The other direction: the name being pointed AT is one we already resolve.
    ms.record_alias(session, alias="TI", canonical="Texas Instruments")
    ms.record_alias(session, alias="TEXAS", canonical="TI")
    assert ms.canonical_name(session, "TEXAS") == "Texas Instruments"


def test_the_repointing_is_written_not_just_held_in_the_session(
    session: Session, engine
) -> None:  # type: ignore[no-untyped-def]
    # The rows moved by the collapse are a second write, after record_alias has
    # already committed its own. Read them back through a FRESH session, or the
    # test passes on objects the first session is simply still holding.
    ms.record_alias(session, alias="TI", canonical="Texas Instr")
    ms.record_alias(session, alias="Texas Instr", canonical="Texas Instruments")

    with Session(engine) as fresh:
        assert ms.canonical_name(fresh, "TI") == "Texas Instruments"


def test_a_cycle_cannot_be_recorded(session: Session) -> None:
    # Pointing the canonical name back at its own alias resolves to itself, which
    # is the "nothing to record" case rather than a loop for the lookup to walk.
    ms.record_alias(session, alias="TI", canonical="Texas Instruments")
    assert ms.record_alias(session, alias="Texas Instruments", canonical="TI") is None
    assert ms.canonical_name(session, "TI") == "Texas Instruments"


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
    hit = cs.find_duplicate_component(session, mpn="MCP2200", manufacturer="MICROCHIP")
    assert hit is not None
    assert hit.manufacturer == "Microchip Technology"


def test_two_makers_sharing_an_mpn_are_still_two_parts(session: Session) -> None:
    # The reason nothing is inferred: an MPN is not unique across manufacturers.
    _part(session, mpn="5120", manufacturer="Keystone Electronics")
    assert cs.find_duplicate_component(session, mpn="5120", manufacturer="ABB") is None


# --- the candidates the user is asked about ----------------------------------


def test_a_differently_named_maker_is_offered(session: Session) -> None:
    existing = _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    found = cs.find_parts_sharing_mpn(session, "MCP2200")
    assert [c.id for c in found] == [existing.id]
    assert found[0].manufacturer == "Microchip Technology"


def test_an_exact_match_is_offered_too(session: Session) -> None:
    """The rule this used to break: a same-maker hit was left out as "not an
    ambiguity".

    It is a duplicate rather than an ambiguity, true — and the create endpoint
    refuses it anyway. But that refusal lands after the form is filled in, which is
    when it is least welcome, and "I already have this part number" is the same news
    either way.
    """
    existing = _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    found = cs.find_parts_sharing_mpn(session, "MCP2200")
    assert [c.id for c in found] == [existing.id]


def test_an_alias_does_not_hide_a_part_you_already_own(session: Session) -> None:
    # With the alias, "MICROCHIP" now RESOLVES to the stored maker — which makes
    # this a duplicate, not a question about naming. Still worth saying: the user
    # is one click from creating a second copy of a part they have.
    _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    ms.record_alias(session, alias="MICROCHIP", canonical="Microchip Technology")
    assert len(cs.find_parts_sharing_mpn(session, "MCP2200")) == 1


def test_a_part_with_no_manufacturer_is_offered(session: Session) -> None:
    # A Farnell invoice prints no maker at all, so nothing about the maker can
    # narrow this — the part number is the whole of the evidence.
    existing = _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    assert [c.id for c in cs.find_parts_sharing_mpn(session, "MCP2200")] == [
        existing.id
    ]


def test_an_unknown_mpn_asks_nothing(session: Session) -> None:
    _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    assert cs.find_parts_sharing_mpn(session, "NX3P1108") == []
    # A half-typed field must not reach the query at all, and None must not reach
    # .lower() — the dialog calls this as the MPN settles, blank included.
    assert cs.find_parts_sharing_mpn(session, None) == []
    assert cs.find_parts_sharing_mpn(session, "   ") == []


def test_the_part_number_is_trimmed_before_it_is_looked_up(session: Session) -> None:
    # The whole job of the guard beyond the blank check: a field the user pasted
    # into carries whitespace, and the lookup is an exact (case-folded) equality —
    # so an untrimmed value silently matches nothing.
    existing = _part(session, mpn="MCP2200", manufacturer="Microchip Technology")
    assert [c.id for c in cs.find_parts_sharing_mpn(session, "  MCP2200 ")] == [
        existing.id
    ]


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

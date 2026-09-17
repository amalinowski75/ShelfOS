"""Service tests for grouping catalogue entries that are one physical part."""

from __future__ import annotations

from typing import cast

import pytest
from app.models.equivalence import (
    ComponentEquivalenceGroup,
    ComponentEquivalenceMember,
)
from app.services import component_service as cs
from app.services import equivalence_service as es
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session, select


def _part(session: Session, mpn: str, *, manufacturer: str | None = None) -> int:
    """A live component with an MPN, which is what a group is made of."""
    ctype = cs.create_type(session, f"type-{mpn}")
    component = cs.create_component(
        session, cast(int, ctype.id), mpn=mpn, manufacturer=manufacturer
    )
    return cast(int, component.id)


def _members(session: Session) -> list[ComponentEquivalenceMember]:
    return list(session.exec(select(ComponentEquivalenceMember)).all())


def _groups(session: Session) -> list[ComponentEquivalenceGroup]:
    return list(session.exec(select(ComponentEquivalenceGroup)).all())


# --- saying two entries are one part ---------------------------------------


def test_an_ungrouped_part_is_equivalent_to_itself_alone(session: Session) -> None:
    part = _part(session, "AO3400A")
    assert es.equivalent_ids(session, part) == [part]
    assert es.group_for(session, part) is None


def test_linking_two_parts_makes_each_find_the_other(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")

    group = es.link_components(session, tape, bulk, user_id=1)

    assert sorted(es.equivalent_ids(session, tape)) == sorted([tape, bulk])
    # Read from the OTHER side too: the group is a fact about both entries, not a
    # pointer the part it was created from happens to own.
    assert sorted(es.equivalent_ids(session, bulk)) == sorted([tape, bulk])
    assert es.group_for(session, bulk) is not None
    assert cast(ComponentEquivalenceGroup, es.group_for(session, bulk)).id == group.id


def test_a_third_variant_joins_the_existing_group(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    tray = _part(session, "AO3400A/TRAY")

    first = es.link_components(session, tape, bulk, user_id=1)
    second = es.link_components(session, bulk, tray, user_id=1)

    assert first.id == second.id  # one group, not two
    assert len(es.equivalent_ids(session, tray)) == 3
    assert len(_groups(session)) == 1


def test_linking_the_same_pair_twice_changes_nothing(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    first = es.link_components(session, tape, bulk, user_id=1)

    again = es.link_components(session, tape, bulk, user_id=1)

    assert again.id == first.id
    assert len(_members(session)) == 2  # no duplicate membership row


def test_a_part_cannot_be_the_same_as_itself(session: Session) -> None:
    part = _part(session, "AO3400A")
    with pytest.raises(ValidationError):
        es.link_components(session, part, part, user_id=1)


def test_a_part_already_in_another_group_is_refused_by_name(
    session: Session,
) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    other = _part(session, "SI2302")
    spare = _part(session, "SI2302-TR")
    es.link_components(session, tape, bulk, user_id=1)
    es.link_components(session, other, spare, user_id=1)

    with pytest.raises(ValidationError) as excinfo:
        es.link_components(session, tape, other, user_id=1)

    # Named, because "it is already in a group" without saying which leaves the
    # user with nothing to act on.
    assert "SI2302" in str(excinfo.value)
    assert len(_groups(session)) == 2  # nothing merged behind their back


def test_a_retired_part_cannot_be_grouped(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    cs.soft_delete_component(session, bulk, reason="gone", user_id=1)

    with pytest.raises(ValidationError):
        es.link_components(session, tape, bulk, user_id=1)
    # And from the other direction: the deleted part is not a place to hang a
    # group off either.
    with pytest.raises(ValidationError):
        es.link_components(session, bulk, tape, user_id=1)


def test_linking_a_part_that_does_not_exist_is_a_not_found(
    session: Session,
) -> None:
    part = _part(session, "AO3400A")
    with pytest.raises(NotFoundError):
        es.link_components(session, part, 9999, user_id=1)


# --- the note that says why ------------------------------------------------


def test_the_note_is_kept_and_not_overwritten_by_a_later_add(
    session: Session,
) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    tray = _part(session, "AO3400A/TRAY")

    es.link_components(session, tape, bulk, user_id=1, notes="same die")
    es.link_components(session, tape, tray, user_id=1, notes="I typed something")

    group = cast(ComponentEquivalenceGroup, es.group_for(session, tape))
    assert group.notes == "same die"


def test_a_note_fills_a_blank_one_on_an_existing_group(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    tray = _part(session, "AO3400A/TRAY")
    es.link_components(session, tape, bulk, user_id=1)

    es.link_components(session, tape, tray, user_id=1, notes="tape, bulk and tray")

    group = cast(ComponentEquivalenceGroup, es.group_for(session, tape))
    assert group.notes == "tape, bulk and tray"


def test_set_notes_rewrites_and_a_blank_clears(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    group = es.link_components(session, tape, bulk, user_id=1, notes="first")

    es.set_notes(session, cast(int, group.id), notes="  second  ")
    reread = cast(ComponentEquivalenceGroup, es.group_for(session, tape))
    assert reread.notes == "second"  # and trimmed

    es.set_notes(session, cast(int, group.id), notes="   ")
    reread = cast(ComponentEquivalenceGroup, es.group_for(session, tape))
    assert reread.notes is None


def test_an_over_long_note_is_refused(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    with pytest.raises(ValidationError):
        es.link_components(session, tape, bulk, user_id=1, notes="x" * 2001)


# --- taking a part back out ------------------------------------------------


def test_removing_one_of_two_dissolves_the_group(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    es.link_components(session, tape, bulk, user_id=1)

    es.unlink_component(session, bulk)

    # Both rows AND the group itself: a group of one says nothing, and left
    # standing it would adopt whatever was added to `tape` next.
    assert _members(session) == []
    assert _groups(session) == []
    assert es.equivalent_ids(session, tape) == [tape]


def test_removing_one_of_three_leaves_the_others_grouped(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    tray = _part(session, "AO3400A/TRAY")
    es.link_components(session, tape, bulk, user_id=1)
    es.link_components(session, tape, tray, user_id=1)

    es.unlink_component(session, tray)

    assert sorted(es.equivalent_ids(session, tape)) == sorted([tape, bulk])
    assert len(_groups(session)) == 1
    assert es.equivalent_ids(session, tray) == [tray]


def test_removing_an_ungrouped_part_is_refused(session: Session) -> None:
    part = _part(session, "AO3400A")
    with pytest.raises(ValidationError):
        es.unlink_component(session, part)


def test_a_hard_deleted_part_takes_its_membership_with_it(
    session: Session,
) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    es.link_components(session, tape, bulk, user_id=1)

    cs.hard_delete_component(session, bulk)

    assert es.equivalent_ids(session, tape) == [tape]
    assert _members(session) == []
    assert _groups(session) == []


def test_a_soft_deleted_part_keeps_its_place(session: Session) -> None:
    tape = _part(session, "AO3400A-TR")
    bulk = _part(session, "AO3400A")
    es.link_components(session, tape, bulk, user_id=1)

    cs.soft_delete_component(session, bulk, reason="gone", user_id=1)

    # The decision survives, because taking a part out of use is reversible and
    # the restore could not bring the grouping back on its own.
    assert sorted(es.equivalent_ids(session, tape)) == sorted([tape, bulk])


# --- finding the variants to pick from -------------------------------------


def test_candidates_match_a_substring_of_the_mpn(session: Session) -> None:
    bulk = _part(session, "AO3400A")
    _part(session, "AO3400A-TR")
    _part(session, "SI2302")

    found = [c.mpn for c in es.search_candidates(session, bulk, "AO3400")]

    # The whole point: "AO3400A" has to find "AO3400A-TR", and the part searching
    # is never offered as its own variant.
    assert found == ["AO3400A-TR"]


def test_candidates_can_be_found_by_manufacturer(session: Session) -> None:
    part = _part(session, "AO3400A")
    _part(session, "XYZ-1", manufacturer="Alpha & Omega")

    found = [c.mpn for c in es.search_candidates(session, part, "omega")]

    assert found == ["XYZ-1"]


def test_candidates_leave_out_the_current_group_and_retired_parts(
    session: Session,
) -> None:
    bulk = _part(session, "AO3400A")
    tape = _part(session, "AO3400A-TR")
    tray = _part(session, "AO3400A/TRAY")
    gone = _part(session, "AO3400A-OLD")
    es.link_components(session, bulk, tape, user_id=1)
    cs.soft_delete_component(session, gone, reason="gone", user_id=1)

    found = [c.mpn for c in es.search_candidates(session, bulk, "AO3400")]

    assert found == ["AO3400A/TRAY"]
    assert tray in {c.id for c in es.search_candidates(session, bulk, "AO3400")}


def test_a_candidate_in_another_group_is_still_offered(session: Session) -> None:
    bulk = _part(session, "AO3400A")
    other = _part(session, "AO3400A-TR")
    spare = _part(session, "AO3400A/TRAY")
    es.link_components(session, other, spare, user_id=1)

    found = {c.mpn for c in es.search_candidates(session, bulk, "AO3400")}

    # Returned so the picker can show it as unavailable and say why; hiding it
    # would leave the searcher hunting for a part plainly in the catalogue.
    assert found == {"AO3400A-TR", "AO3400A/TRAY"}


def test_a_wildcard_in_the_query_is_searched_for_literally(
    session: Session,
) -> None:
    part = _part(session, "AO3400A")
    _part(session, "50%RH-SENSOR")
    _part(session, "SI2302")

    found = [c.mpn for c in es.search_candidates(session, part, "%")]

    # Unescaped, "%" is LIKE's own wildcard and this would return the whole
    # catalogue — the search would look like it worked while meaning nothing.
    assert found == ["50%RH-SENSOR"]


def test_an_underscore_in_the_query_is_searched_for_literally(
    session: Session,
) -> None:
    part = _part(session, "AO3400A")
    _part(session, "RES_1K")
    _part(session, "RESX1K")

    found = [c.mpn for c in es.search_candidates(session, part, "RES_1K")]

    assert found == ["RES_1K"]


def test_a_blank_query_finds_nothing(session: Session) -> None:
    part = _part(session, "AO3400A")
    _part(session, "AO3400A-TR")
    assert es.search_candidates(session, part, "   ") == []


def test_candidates_stop_at_the_limit(session: Session) -> None:
    part = _part(session, "BASE")
    for index in range(5):
        _part(session, f"BASE-{index}")

    assert len(es.search_candidates(session, part, "BASE", limit=3)) == 3

"""Taking a whole BOM off the shelves: where the parts come from, and the record.

The fixtures build the workflow the feature exists for: a temporary "gathering"
branch the parts are collected into before assembly, and the ordinary shelves
they normally live on.
"""

from __future__ import annotations

from typing import cast

import pytest
from app.models.enums import LocationType, StockReason
from app.models.stock import StockMovement
from app.seed import ensure_demo_user
from app.services import bom_service as bs
from app.services import bom_take_service as bts
from app.services import component_service as cs
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session, col, select


@pytest.fixture
def shop(session: Session):  # type: ignore[no-untyped-def]
    """A gathering branch, two ordinary shelves, and factories for parts/BOMs."""
    ensure_demo_user(session)
    ctype = cs.create_type(session, "ic")
    gathering = ls.create_location(session, type=LocationType.BOX, name="Kontroler CNC")
    resistors = ls.create_location(
        session, type=LocationType.DRAWER, name="Rezystory", parent_id=gathering.id
    )
    connectors = ls.create_location(
        session, type=LocationType.DRAWER, name="Zlacza", parent_id=gathering.id
    )
    shelf_a = ls.create_location(session, type=LocationType.SHELF, name="Regal A")
    shelf_b = ls.create_location(session, type=LocationType.SHELF, name="Regal B")

    class Shop:
        gathering_id = cast(int, gathering.id)
        resistors_id = cast(int, resistors.id)
        connectors_id = cast(int, connectors.id)
        shelf_a_id = cast(int, shelf_a.id)
        shelf_b_id = cast(int, shelf_b.id)

        @staticmethod
        def part(mpn: str) -> int:
            component = cs.create_component_with_values(
                session, ctype.id, mpn=mpn
            )
            return cast(int, component.id)

        @staticmethod
        def stock(component_id: int, location_id: int, quantity: int) -> None:
            ss.add_stock(
                session,
                component_id=component_id,
                location_id=location_id,
                quantity=quantity,
                user_id=1,
            )

        @staticmethod
        def bom(rows: str, *, name: str = "Kontroler CNC"):  # type: ignore[no-untyped-def]
            header = b"Reference,Qty,Value,MPN\n"
            return bs.create_bom(
                session,
                name=name,
                filename="b.csv",
                data=header + rows.encode(),
                user_id=1,
            )

        @staticmethod
        def assign(bom_id: int, references: str, component_id: int) -> None:
            line = next(
                ln for ln in bs.get_bom_lines(session, bom_id)
                if ln.references == references
            )
            bs.assign_component(
                session,
                bom_id,
                cast(int, line.id),
                component_id=component_id,
                user_id=1,
            )

    return Shop


def _line_id(session: Session, bom) -> int:  # type: ignore[no-untyped-def]
    """The id of the BOM's first line — what overrides and choices are keyed by."""
    return cast(int, bs.get_bom_lines(session, cast(int, bom.id))[0].id)


def _no_removals(session: Session) -> bool:
    """No stock has left any shelf — what a refused or rolled-back run must leave."""
    return (
        session.exec(
            select(StockMovement).where(col(StockMovement.delta_quantity) < 0)
        ).first()
        is None
    )


def _one_line(  # type: ignore[no-untyped-def]
    session: Session, shop, *, stocked: dict[int, int], qty: int = 1
):
    """A one-line BOM for "U1", assigned, with the given stock per location."""
    part = shop.part("PART-A")
    for location_id, quantity in stocked.items():
        shop.stock(part, location_id, quantity)
    bom = shop.bom(f"U1,{qty},x,PART-A\n")
    shop.assign(cast(int, bom.id), "U1", part)
    return bom, part


# --- where the parts come from ---------------------------------------------


def test_the_gathering_branch_is_drained_before_any_shelf(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    """The whole point of gathering: what was put out for the board is used first."""
    bom, part = _one_line(
        session, shop, stocked={shop.resistors_id: 10, shop.shelf_a_id: 100}, qty=4
    )

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    line = plan.lines[0]
    assert [(s.location_id, s.quantity) for s in line.sources] == [
        (shop.resistors_id, 4)
    ]
    assert line.shortfall == 0 and plan.can_run


def test_it_spreads_across_the_sub_containers_without_asking(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    """They are all "the parts I put out for this board"; largest bin first."""
    bom, part = _one_line(
        session, shop, stocked={shop.resistors_id: 3, shop.connectors_id: 9}, qty=10
    )

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    assert [(s.location_id, s.quantity) for s in plan.lines[0].sources] == [
        (shop.connectors_id, 9),  # the bigger bin first
        (shop.resistors_id, 1),  # then only what the first could not cover
    ]
    assert plan.lines[0].needs_choice is False


def test_the_branch_is_exhausted_before_the_shelf_tops_it_up(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    """A partly gathered line is not "not there" — it is short by the difference."""
    bom, part = _one_line(
        session, shop, stocked={shop.resistors_id: 4, shop.shelf_a_id: 50}, qty=10
    )

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    assert [(s.location_id, s.quantity) for s in plan.lines[0].sources] == [
        (shop.resistors_id, 4),
        (shop.shelf_a_id, 6),
    ]


def test_one_place_outside_is_taken_without_a_question(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(session, shop, stocked={shop.shelf_a_id: 50}, qty=10)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    assert [(s.location_id, s.quantity) for s in plan.lines[0].sources] == [
        (shop.shelf_a_id, 10)
    ]
    assert plan.can_run


def test_two_places_outside_are_a_question_for_a_person(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(
        session, shop, stocked={shop.shelf_a_id: 50, shop.shelf_b_id: 50}, qty=10
    )

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    line = plan.lines[0]
    assert line.needs_choice is True
    assert line.sources == []  # nothing is planned until the question is answered
    assert {c.location_id for c in line.candidates} == {
        shop.shelf_a_id,
        shop.shelf_b_id,
    }
    assert plan.can_run is False
    assert plan.unanswered_references == ["U1"]


def test_a_chosen_location_is_not_spilled_into_the_others(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    """Answering "take it from B" must not quietly take the rest from A too."""
    bom, part = _one_line(
        session, shop, stocked={shop.shelf_a_id: 50, shop.shelf_b_id: 4}, qty=10
    )

    plan = bts.plan_take(
        session,
        cast(int, bom.id),
        boards=1,
        source_location_id=shop.gathering_id,
        choices={_line_id(session, bom): shop.shelf_b_id},
    )

    line = plan.lines[0]
    assert [(s.location_id, s.quantity) for s in line.sources] == [(shop.shelf_b_id, 4)]
    assert line.shortfall == 6  # …and the rest is short, not silently taken from A
    assert plan.can_run


def test_a_choice_naming_a_location_without_stock_is_refused(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(
        session, shop, stocked={shop.shelf_a_id: 50, shop.shelf_b_id: 4}, qty=10
    )
    with pytest.raises(ValidationError):
        bts.plan_take(
            session,
            cast(int, bom.id),
            boards=1,
            source_location_id=shop.gathering_id,
            choices={_line_id(session, bom): shop.connectors_id},
        )


def test_a_choice_another_line_drained_is_a_shortfall_not_a_refusal(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """The answer was true when it was given; a later line taking the lot is not
    the user being wrong, and refusing the whole run over it blocks the preview."""
    part = shop.part("PART-A")
    shop.stock(part, shop.shelf_a_id, 10)  # exactly one line's worth
    shop.stock(part, shop.shelf_b_id, 50)
    bom = shop.bom("R1,10,x,PART-A\nR2,10,x,PART-A\n")
    shop.assign(cast(int, bom.id), "R1", part)
    shop.assign(cast(int, bom.id), "R2", part)
    lines = bs.get_bom_lines(session, cast(int, bom.id))
    both_on_a = {cast(int, ln.id): shop.shelf_a_id for ln in lines}

    plan = bts.plan_take(
        session,
        cast(int, bom.id),
        boards=1,
        source_location_id=shop.gathering_id,
        choices=both_on_a,
    )

    assert [(s.location_id, s.quantity) for s in plan.lines[0].sources] == [
        (shop.shelf_a_id, 10)
    ]
    assert plan.lines[1].sources == []  # A is empty, and R1's answer stands
    assert plan.lines[1].shortfall == 10
    assert plan.can_run  # …and the run is still possible for everything else


def test_the_candidates_stay_offered_after_the_choice_is_made(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """A picker that vanishes the moment it is used cannot be corrected."""
    bom, part = _one_line(
        session, shop, stocked={shop.shelf_a_id: 50, shop.shelf_b_id: 50}, qty=10
    )
    line_id = _line_id(session, bom)

    answered = bts.plan_take(
        session,
        cast(int, bom.id),
        boards=1,
        source_location_id=shop.gathering_id,
        choices={line_id: shop.shelf_b_id},
    )

    line = answered.lines[0]
    assert line.needs_choice is False  # the question is answered…
    assert {c.location_id for c in line.candidates} == {
        shop.shelf_a_id,
        shop.shelf_b_id,
    }  # …but still askable


def test_nothing_anywhere_is_a_shortfall_not_an_error(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(session, shop, stocked={}, qty=10)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    assert plan.lines[0].sources == [] and plan.lines[0].shortfall == 10
    assert plan.can_run  # the rest of the board can still be picked


def test_two_lines_sharing_a_component_do_not_plan_the_same_bin_twice(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """An assignment is unique per designator group, not per component.

    Two groups pointing at the same part each used to plan against the bin's full
    quantity: the preview promised a run it was 20 short for, and the second
    removal then raised mid-take and rolled the whole thing back with the very
    stock error the plan had ruled out.
    """
    part = shop.part("PART-A")
    shop.stock(part, shop.resistors_id, 100)
    bom = shop.bom("R1,60,x,PART-A\nR2,60,x,PART-A\n")
    shop.assign(cast(int, bom.id), "R1", part)
    shop.assign(cast(int, bom.id), "R2", part)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    first, second = plan.lines
    assert [(s.location_id, s.quantity) for s in first.sources] == [
        (shop.resistors_id, 60)
    ]
    assert [(s.location_id, s.quantity) for s in second.sources] == [
        (shop.resistors_id, 40)  # what is LEFT, not what the shelf started with
    ]
    assert second.shortfall == 20

    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    assert ss.get_quantity(session, part, shop.resistors_id) == 0
    lines = bts.take_lines(session, cast(int, take.id))
    assert [ln.taken_quantity for ln in lines] == [60, 40]


def test_a_bin_emptied_by_an_earlier_line_is_not_offered_again(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """…and the second line falls through to the shelf, rather than to nothing."""
    part = shop.part("PART-A")
    shop.stock(part, shop.resistors_id, 10)
    shop.stock(part, shop.shelf_a_id, 50)
    bom = shop.bom("R1,10,x,PART-A\nR2,5,x,PART-A\n")
    shop.assign(cast(int, bom.id), "R1", part)
    shop.assign(cast(int, bom.id), "R2", part)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )

    assert [(s.location_id, s.quantity) for s in plan.lines[0].sources] == [
        (shop.resistors_id, 10)
    ]
    assert [(s.location_id, s.quantity) for s in plan.lines[1].sources] == [
        (shop.shelf_a_id, 5)
    ]
    assert plan.total_shortfall == 0


# --- what is asked for ------------------------------------------------------


def test_boards_multiply_what_each_line_asks_for(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 100}, qty=4)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=3, source_location_id=shop.gathering_id
    )

    assert plan.lines[0].per_board == 4
    assert plan.lines[0].requested == 12


def test_a_typed_quantity_beats_the_board_count_in_both_directions(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """Parts get lost during assembly, and sometimes fewer are wanted."""
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 100}, qty=4)
    line_id = _line_id(session, bom)

    more = bts.plan_take(
        session, cast(int, bom.id), boards=3,
        source_location_id=shop.gathering_id, overrides={line_id: 15},
    )
    assert more.lines[0].requested == 15

    fewer = bts.plan_take(
        session, cast(int, bom.id), boards=3,
        source_location_id=shop.gathering_id, overrides={line_id: 2},
    )
    assert fewer.lines[0].requested == 2


# --- what is refused --------------------------------------------------------


def test_an_unassigned_line_refuses_the_whole_run(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    """Taking most of a board and reporting success is worse than taking none."""
    good = shop.part("PART-A")
    shop.stock(good, shop.resistors_id, 100)
    bom = shop.bom("U1,1,x,PART-A\nU2,1,x,PART-B\n")
    shop.assign(cast(int, bom.id), "U1", good)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )
    assert plan.blocked_references == ["U2"]
    assert plan.lines[1].blocked == bts.BLOCKED_UNASSIGNED

    with pytest.raises(ValidationError):
        bts.execute_take(
            session, cast(int, bom.id), boards=1,
            source_location_id=shop.gathering_id, user_id=1,
        )

    assert _no_removals(session)
    assert bts.list_takes(session, cast(int, bom.id)) == []


def test_a_retired_component_is_refused_in_the_plan_not_mid_run(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """Otherwise it raises from inside the loop, with half the shelf emptied."""
    bom, part = _one_line(session, shop, stocked={}, qty=1)
    cs.soft_delete_component(session, part, user_id=1)

    plan = bts.plan_take(
        session, cast(int, bom.id), boards=1, source_location_id=shop.gathering_id
    )
    assert plan.lines[0].blocked == bts.BLOCKED_RETIRED
    with pytest.raises(ValidationError):
        bts.execute_take(
            session, cast(int, bom.id), boards=1,
            source_location_id=shop.gathering_id, user_id=1,
        )


def test_an_unanswered_choice_refuses_the_run(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(
        session, shop, stocked={shop.shelf_a_id: 50, shop.shelf_b_id: 50}, qty=10
    )

    with pytest.raises(ValidationError):
        bts.execute_take(
            session, cast(int, bom.id), boards=1,
            source_location_id=shop.gathering_id, user_id=1,
        )


def test_an_unknown_bom_or_location_is_not_found(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(session, shop, stocked={}, qty=1)
    with pytest.raises(NotFoundError):
        bts.plan_take(session, 9999, boards=1, source_location_id=shop.gathering_id)
    with pytest.raises(NotFoundError):
        bts.plan_take(session, cast(int, bom.id), boards=1, source_location_id=9999)


# --- running it -------------------------------------------------------------


def test_the_take_removes_the_stock_and_records_where_it_came_from(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    bom, part = _one_line(
        session, shop, stocked={shop.resistors_id: 3, shop.shelf_a_id: 50}, qty=10
    )

    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    assert ss.get_quantity(session, part, shop.resistors_id) == 0
    assert ss.get_quantity(session, part, shop.shelf_a_id) == 43
    lines = bts.take_lines(session, cast(int, take.id))
    assert [
        (ln.references, ln.requested_quantity, ln.taken_quantity) for ln in lines
    ] == [("U1", 10, 10)]
    allocations = bts.take_allocations(session, cast(int, take.id))
    assert [(a.location_id, a.quantity) for a in allocations] == [
        (shop.resistors_id, 3),
        (shop.shelf_a_id, 7),
    ]
    # Every allocation names a real movement of the right sign and size.
    for allocation in allocations:
        movement = session.get(StockMovement, allocation.movement_id)
        assert movement is not None
        assert movement.delta_quantity == -allocation.quantity
        assert movement.reason is StockReason.USAGE
        assert movement.note == take.name
    assert ss.verify_cache_consistency(session) is True


def test_the_snapshot_is_named_after_the_bom_and_the_moment(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=1)

    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    assert take.name.startswith("Kontroler CNC ")
    assert take.created_at.strftime("%Y-%m-%d") in take.name
    # Stored, not derived: renaming the BOM must not make old movement notes lie.
    bom.name = "Something else"
    session.add(bom)
    session.commit()
    assert bts.get_take(session, cast(int, take.id)).name == take.name


def test_a_shortfall_is_recorded_and_the_other_lines_are_unaffected(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    plenty = shop.part("PART-A")
    scarce = shop.part("PART-B")
    shop.stock(plenty, shop.resistors_id, 100)
    shop.stock(scarce, shop.resistors_id, 2)
    bom = shop.bom("U1,5,x,PART-A\nU2,5,x,PART-B\n")
    shop.assign(cast(int, bom.id), "U1", plenty)
    shop.assign(cast(int, bom.id), "U2", scarce)

    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    lines = {ln.references: ln for ln in bts.take_lines(session, cast(int, take.id))}
    assert (lines["U1"].requested_quantity, lines["U1"].taken_quantity) == (5, 5)
    assert (lines["U2"].requested_quantity, lines["U2"].taken_quantity) == (5, 2)
    assert ss.get_quantity(session, scarce, shop.resistors_id) == 0


def test_every_line_gets_a_row_even_when_it_moved_nothing(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """The snapshot records the whole decision, not only what happened to move."""
    bom, part = _one_line(session, shop, stocked={}, qty=5)

    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    lines = bts.take_lines(session, cast(int, take.id))
    assert [(ln.requested_quantity, ln.taken_quantity) for ln in lines] == [(5, 0)]
    assert bts.take_allocations(session, cast(int, take.id)) == []


def test_a_failure_partway_leaves_nothing_behind(
    session: Session, shop, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """All or nothing: a half-emptied shelf with no record is the worst outcome."""
    first = shop.part("PART-A")
    second = shop.part("PART-B")
    shop.stock(first, shop.resistors_id, 100)
    shop.stock(second, shop.resistors_id, 100)
    bom = shop.bom("U1,5,x,PART-A\nU2,5,x,PART-B\n")
    shop.assign(cast(int, bom.id), "U1", first)
    shop.assign(cast(int, bom.id), "U2", second)

    real = ss.remove_stock
    calls = {"n": 0}

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the shelf collapsed")
        return real(*args, **kwargs)

    monkeypatch.setattr(bts.ss, "remove_stock", explode)

    with pytest.raises(RuntimeError):
        bts.execute_take(
            session, cast(int, bom.id), boards=1,
            source_location_id=shop.gathering_id, user_id=1,
        )

    assert ss.get_quantity(session, first, shop.resistors_id) == 100
    assert ss.get_quantity(session, second, shop.resistors_id) == 100
    assert bts.list_takes(session, cast(int, bom.id)) == []
    assert _no_removals(session)


# --- undoing it -------------------------------------------------------------


def test_reversing_puts_every_part_back_where_it_came_from(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    bom, part = _one_line(
        session, shop, stocked={shop.resistors_id: 3, shop.shelf_a_id: 50}, qty=10
    )
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    reversed_take = bts.reverse_take(
        session, cast(int, take.id), reason="board scrapped", user_id=1
    )

    assert ss.get_quantity(session, part, shop.resistors_id) == 3
    assert ss.get_quantity(session, part, shop.shelf_a_id) == 50
    assert ss.verify_cache_consistency(session) is True
    # Marked, never deleted: what happened still happened.
    assert reversed_take.reversed_at is not None
    assert reversed_take.reversal_reason == "board scrapped"
    assert bts.take_lines(session, cast(int, take.id)) != []
    for allocation in bts.take_allocations(session, cast(int, take.id)):
        movement = session.get(StockMovement, allocation.reversal_movement_id)
        assert movement is not None
        assert movement.delta_quantity == allocation.quantity
        assert movement.reason is StockReason.CORRECTION
        assert "board scrapped" in cast(str, movement.note)


def test_a_reversal_with_no_reason_moves_nothing(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    """A reversal that does not say what happened is a hole in the record."""
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=4)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    with pytest.raises(ValidationError):
        bts.reverse_take(session, cast(int, take.id), reason="   ", user_id=1)

    assert ss.get_quantity(session, part, shop.resistors_id) == 6
    assert bts.get_take(session, cast(int, take.id)).reversed_at is None


def test_reversing_twice_returns_the_stock_once(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=4)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )
    bts.reverse_take(session, cast(int, take.id), reason="scrapped", user_id=1)

    with pytest.raises(ValidationError):
        bts.reverse_take(session, cast(int, take.id), reason="again", user_id=1)

    assert ss.get_quantity(session, part, shop.resistors_id) == 10


def test_a_reversal_blocked_by_a_retired_part_says_which_line(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """`add_stock` refuses a retired component, so this must be caught up front.

    Hit mid-loop it would roll back the reversal — the mark included — leaving the
    other lines off the shelves and a message naming neither the take nor the line.
    """
    good = shop.part("PART-A")
    doomed = shop.part("PART-B")
    shop.stock(good, shop.resistors_id, 100)
    shop.stock(doomed, shop.resistors_id, 100)
    bom = shop.bom("U1,5,x,PART-A\nU2,5,x,PART-B\n")
    shop.assign(cast(int, bom.id), "U1", good)
    shop.assign(cast(int, bom.id), "U2", doomed)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )
    # Emptying the drawer is what the service demands before a part goes out of
    # use, and it is the real order of events after a take anyway.
    ss.remove_stock(
        session,
        component_id=doomed,
        location_id=shop.resistors_id,
        quantity=95,
        user_id=1,
    )
    cs.soft_delete_component(session, doomed, user_id=1)

    with pytest.raises(ValidationError, match="U2"):
        bts.reverse_take(session, cast(int, take.id), reason="scrapped", user_id=1)

    # Nothing half-done: the mark is not set and the live part stays taken.
    assert bts.get_take(session, cast(int, take.id)).reversed_at is None
    assert ss.get_quantity(session, good, shop.resistors_id) == 95


def test_reversing_an_unknown_take_is_not_found(  # type: ignore[no-untyped-def]
    session: Session, shop
) -> None:
    with pytest.raises(NotFoundError):
        bts.reverse_take(session, 9999, reason="whatever", user_id=1)


# --- finding the snapshot from a movement -----------------------------------


def test_takes_by_movement_asks_in_chunks(
    session: Session, shop, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Each id is bound TWICE — once per arm of the OR — so the bind ceiling
    arrives at half the movement count it looks like.

    The caller is the component page, whose movement list is uncapped: without
    chunking, a well-used part takes the whole page down with "too many SQL
    variables" rather than just losing its note links. Asserted by counting the
    queries with the chunk shrunk to 2, because the ceiling itself is a property
    of whichever SQLite the machine has (999 on older builds, 32766 since 3.32) —
    a test that fed it 1500 ids would pass here and fail on a colleague's laptop.
    """
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=1)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )
    real = bts.take_allocations(session, cast(int, take.id))[0].movement_id
    monkeypatch.setattr(bts, "_MOVEMENT_CHUNK", 2)
    queries = []
    real_exec = session.exec

    def counting_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        queries.append(1)
        return real_exec(*args, **kwargs)

    monkeypatch.setattr(session, "exec", counting_exec)

    found = bts.takes_by_movement(session, [*range(10_000, 10_007), real])

    assert len(queries) == 4  # eight ids, two at a time
    assert list(found) == [real]  # …and the one real id is still found


def test_a_movement_maps_back_to_its_snapshot_both_ways(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """The link that makes a movement's note clickable, without a schema change."""
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=4)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )
    bts.reverse_take(session, cast(int, take.id), reason="scrapped", user_id=1)
    allocation = bts.take_allocations(session, cast(int, take.id))[0]
    ordinary = ss.add_stock(
        session, component_id=part, location_id=shop.shelf_a_id, quantity=5, user_id=1
    )

    found = bts.takes_by_movement(
        session,
        [
            allocation.movement_id,
            cast(int, allocation.reversal_movement_id),
            cast(int, ordinary.id),
        ],
    )

    assert found[allocation.movement_id].id == take.id
    assert found[cast(int, allocation.reversal_movement_id)].id == take.id
    # Asked about the reversal leg ALONE: with both legs in one call the row is
    # found by its take movement and the reversal mapping comes along for free,
    # so the query's second arm would go untested.
    reversal_only = bts.takes_by_movement(
        session, [cast(int, allocation.reversal_movement_id)]
    )
    assert list(reversal_only) == [allocation.reversal_movement_id]
    assert reversal_only[cast(int, allocation.reversal_movement_id)].id == take.id
    assert cast(int, ordinary.id) not in found  # an ordinary movement links nowhere
    assert bts.takes_by_movement(session, []) == {}


# --- the BOM behind the snapshot --------------------------------------------


def test_a_bom_cannot_be_deleted_while_a_take_of_it_stands(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """Those parts are off the shelves, and the snapshot is the only way back."""
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=4)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )

    with pytest.raises(ValidationError, match=take.name):
        bs.delete_bom(session, cast(int, bom.id))

    assert bs.get_bom(session, cast(int, bom.id)) is not None
    assert bts.take_lines(session, cast(int, take.id)) != []


def test_a_reversed_take_no_longer_holds_its_bom(
    session: Session, shop
) -> None:  # type: ignore[no-untyped-def]
    """Nothing is off the shelves any more, so the BOM is free to go — and the
    snapshot stays behind as the record of what happened."""
    bom, part = _one_line(session, shop, stocked={shop.resistors_id: 10}, qty=4)
    take = bts.execute_take(
        session, cast(int, bom.id), boards=1,
        source_location_id=shop.gathering_id, user_id=1,
    )
    bts.reverse_take(session, cast(int, take.id), reason="scrapped", user_id=1)

    bs.delete_bom(session, cast(int, bom.id))

    assert bts.get_take(session, cast(int, take.id)).name == take.name

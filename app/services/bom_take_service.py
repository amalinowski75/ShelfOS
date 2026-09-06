"""Take a whole BOM off the shelves in one go, and record what that took.

Building a board means walking a BOM by hand: find each part, find where it
lives, remove the right count. This does the walk in one transaction and leaves a
snapshot behind (:mod:`app.models.bom_take`) saying what came off which shelf, so
the run can be audited later and undone if the board is scrapped.

Two rules shape everything here:

* **Only an assigned line is taken.** The report resolves a line by assignment
  alone, and so does this: an MPN matching one inventory entry is a candidate, not
  a decision, and a take driven by a guess empties the wrong bin. A BOM with an
  unresolved line is refused before anything moves.
* **The gathering tree first.** Before assembly the parts are collected into a
  temporary branch ("Kontroler CNC" with "Rezystory", "Kondensatory", …). The take
  is given that parent and drains it before touching the ordinary shelves — and
  when the fallback is ambiguous, it asks rather than choosing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col, select

from app.models.bom_take import BomTake, BomTakeAllocation, BomTakeLine
from app.models.component import Component
from app.models.enums import StockReason
from app.models.location import ComponentLocation, Location
from app.services import bom_service as bs
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services._common import require_entity
from app.services.errors import ValidationError

# Why a line cannot be taken. Both are refusals of the whole run, not of the line:
# a take that quietly skipped a part would produce a board's worth of movements
# and no board.
BLOCKED_UNASSIGNED = "unassigned"
BLOCKED_RETIRED = "component_retired"


@dataclass
class PlannedSource:
    """One location a line's parts will come from, or could come from."""

    location_id: int
    path: str
    available: int
    quantity: int  # what will be taken from here (0 for an unchosen candidate)
    inside: bool  # within the gathering branch


@dataclass
class PlannedLine:
    line_id: int
    references: str
    mpn: str | None
    component_id: int | None
    per_board: int
    requested: int
    sources: list[PlannedSource] = field(default_factory=list)
    candidates: list[PlannedSource] = field(default_factory=list)
    needs_choice: bool = False
    shortfall: int = 0
    blocked: str | None = None


@dataclass
class TakePlan:
    bom_id: int
    bom_name: str
    boards: int
    source_location_id: int
    source_path: str
    lines: list[PlannedLine]

    @property
    def blocked_references(self) -> list[str]:
        return [ln.references for ln in self.lines if ln.blocked]

    @property
    def unanswered_references(self) -> list[str]:
        return [ln.references for ln in self.lines if ln.needs_choice]

    @property
    def can_run(self) -> bool:
        return not self.blocked_references and not self.unanswered_references

    @property
    def total_shortfall(self) -> int:
        return sum(ln.shortfall for ln in self.lines)


def components_by_id(session: Session, ids: set[int]) -> dict[int, Component]:
    """Fetch the assigned components at once, retired ones included — a line
    assigned to a part since taken out of use has to be reported, not skipped."""
    if not ids:
        return {}
    return {
        cast(int, c.id): c
        for c in session.exec(
            select(Component).where(col(Component.id).in_(ids))
        ).all()
    }


def _slots_by_component(
    session: Session, component_ids: set[int]
) -> dict[int, list[ComponentLocation]]:
    """Every stocked slot for these components, in one query rather than per line."""
    if not component_ids:
        return {}
    rows = session.exec(
        select(ComponentLocation)
        .where(col(ComponentLocation.component_id).in_(component_ids))
        .where(col(ComponentLocation.quantity) > 0)
        .order_by(col(ComponentLocation.id))
    ).all()
    slots: dict[int, list[ComponentLocation]] = {}
    for row in rows:
        slots.setdefault(row.component_id, []).append(row)
    return slots


def _by_biggest_bin(source: PlannedSource) -> tuple[int, int]:
    """Largest bin first, id as a deterministic tiebreak.

    Fewest movements, fewest audit rows, and a bin is emptied only when it must
    be. Pinned by a test so the order cannot drift silently.
    """
    return (-source.available, source.location_id)


def plan_take(
    session: Session,
    bom_id: int,
    *,
    boards: int,
    source_location_id: int,
    overrides: dict[int, int] | None = None,
    choices: dict[int, int] | None = None,
) -> TakePlan:
    """Work out what this take would do. Reads only; writes nothing.

    ``overrides`` maps a BOM line id to the quantity the user typed instead of
    ``line.quantity * boards`` — parts get lost during assembly, so the number is
    editable. ``choices`` maps a line id to the location the user picked when
    several outside the gathering branch hold the part.
    """
    boards = max(1, boards)
    overrides = overrides or {}
    choices = choices or {}
    bom = bs.get_bom(session, bom_id)
    source = require_entity(session, Location, source_location_id, "location")
    # Once per take, never per line: this walks the whole location table.
    inside_ids = set(ls.subtree_ids(session, cast(int, source.id)))

    lines = bs.get_bom_lines(session, bom_id)
    assignments = {a.references: a for a in bs.list_assignments(session, bom_id)}
    components = components_by_id(
        session, {a.component_id for a in assignments.values()}
    )
    slots = _slots_by_component(
        session,
        {c.id for c in components.values() if c.deleted_at is None and c.id},
    )
    # What each slot has LEFT as the plan walks the lines. Two designator groups
    # can be assigned to the same component (the assignment is unique per
    # references, not per component), and without this each of them would plan
    # against the bin's full quantity — the preview would promise a run it is
    # short for, and the second removal would raise mid-take and roll the whole
    # thing back with a stock error the plan had just ruled out.
    unclaimed = {
        (slot.component_id, slot.location_id): slot.quantity
        for rows in slots.values()
        for slot in rows
    }
    paths: dict[int, str] = {}

    def path_of(location_id: int) -> str:
        if location_id not in paths:
            paths[location_id] = ls.format_path(session, location_id)
        return paths[location_id]

    planned: list[PlannedLine] = []
    for line in lines:
        line_id = cast(int, line.id)
        entry = PlannedLine(
            line_id=line_id,
            references=line.references,
            mpn=line.mpn,
            component_id=None,
            per_board=line.quantity,
            requested=max(0, overrides.get(line_id, line.quantity * boards)),
        )
        assignment = assignments.get(line.references)
        component = components.get(assignment.component_id) if assignment else None
        if component is None:
            entry.blocked = BLOCKED_UNASSIGNED
            planned.append(entry)
            continue
        if component.deleted_at is not None:
            # Caught here rather than mid-run, where `require_live_component`
            # would raise from inside the loop with half the shelf already emptied.
            entry.blocked = BLOCKED_RETIRED
            entry.component_id = component.id
            planned.append(entry)
            continue

        entry.component_id = component.id
        component_id = cast(int, component.id)

        def claim(source: PlannedSource, amount: int, part: int = component_id) -> int:
            """Take ``amount`` from this slot and keep the running total honest."""
            source.quantity += amount
            unclaimed[(part, source.location_id)] -= amount
            return amount

        candidates = [
            PlannedSource(
                location_id=slot.location_id,
                path=path_of(slot.location_id),
                # What is left after earlier lines, not what the shelf started
                # with. A slot another line has already emptied is not a candidate.
                available=unclaimed[(component_id, slot.location_id)],
                quantity=0,
                inside=slot.location_id in inside_ids,
            )
            for slot in slots.get(component_id, [])
            if unclaimed[(component_id, slot.location_id)] > 0
        ]
        inside = sorted([c for c in candidates if c.inside], key=_by_biggest_bin)
        outside = sorted([c for c in candidates if not c.inside], key=_by_biggest_bin)

        remaining = entry.requested
        # The gathering branch is EXHAUSTED before anything else is considered, so
        # a partly-gathered line tops up from the shelf instead of reading as
        # absent. Spreading across the sub-containers needs no question: they are
        # all "the parts I put out for this board".
        for candidate in inside:
            if remaining <= 0:
                break
            entry.sources.append(candidate)
            remaining -= claim(candidate, min(remaining, candidate.available))

        if remaining > 0 and outside:
            chosen_id = choices.get(line_id)
            if chosen_id is not None:
                picked = next(
                    (c for c in outside if c.location_id == chosen_id), None
                )
                if picked is None:
                    raise ValidationError(
                        f"'{line.references}' does not have stock at the location "
                        "chosen for it"
                    )
                # An explicit choice is honoured for that location ALONE — spilling
                # the rest into the bins the user did not pick would answer a
                # different question than the one they were asked.
                entry.sources.append(picked)
                remaining -= claim(picked, min(remaining, picked.available))
            elif len(outside) == 1:
                only = outside[0]
                entry.sources.append(only)
                remaining -= claim(only, min(remaining, only.available))
            else:
                entry.needs_choice = True
                entry.candidates = outside

        # Not an error, and not a refusal: take what is there and record the rest.
        # "I am short 40 of these" is exactly what the snapshot is for.
        entry.shortfall = entry.requested - sum(s.quantity for s in entry.sources)
        planned.append(entry)

    return TakePlan(
        bom_id=bom_id,
        bom_name=bom.name,
        boards=boards,
        source_location_id=cast(int, source.id),
        source_path=path_of(cast(int, source.id)),
        lines=planned,
    )


def snapshot_name(bom_name: str, when: datetime) -> str:
    """``"Kontroler CNC 2026-09-06 19:41:07"`` — the note every movement carries.

    Seconds included: two runs a minute apart are told apart by the date, two a
    second apart only by this, and the movements table shows nothing else.
    """
    return f"{bom_name} {when:%Y-%m-%d %H:%M:%S}"


def execute_take(
    session: Session,
    bom_id: int,
    *,
    boards: int,
    source_location_id: int,
    overrides: dict[int, int] | None = None,
    choices: dict[int, int] | None = None,
    user_id: int,
) -> BomTake:
    """Run the take: remove the stock and write the snapshot, all or nothing.

    The plan is recomputed here against live stock rather than trusted from the
    client, which sends only the board count, the gathering parent, the edited
    quantities and the chosen locations.
    """
    plan = plan_take(
        session,
        bom_id,
        boards=boards,
        source_location_id=source_location_id,
        overrides=overrides,
        choices=choices,
    )
    if plan.blocked_references:
        raise ValidationError(
            "every line must name a component before its parts can be taken; "
            f"these do not: {', '.join(plan.blocked_references)}"
        )
    if plan.unanswered_references:
        raise ValidationError(
            "these lines are stocked in more than one place outside the "
            "gathering location, so somewhere must be chosen: "
            + ", ".join(plan.unanswered_references)
        )

    # Built once, before the loop, so every movement's note is byte-identical.
    take = BomTake(
        bom_id=bom_id,
        name=snapshot_name(plan.bom_name, datetime.now(UTC)),
        boards=plan.boards,
        source_location_id=plan.source_location_id,
        created_by=user_id,
    )
    try:
        session.add(take)
        session.flush()  # its id, for the children below
        for line in plan.lines:
            take_line = BomTakeLine(
                take_id=cast(int, take.id),
                references=line.references,
                component_id=cast(int, line.component_id),
                requested_quantity=line.requested,
                taken_quantity=sum(s.quantity for s in line.sources),
            )
            session.add(take_line)
            session.flush()
            for source in line.sources:
                movement = ss.remove_stock(
                    session,
                    component_id=cast(int, line.component_id),
                    location_id=source.location_id,
                    quantity=source.quantity,
                    user_id=user_id,
                    reason=StockReason.USAGE,
                    note=take.name,
                    commit=False,
                )
                session.add(
                    BomTakeAllocation(
                        take_id=cast(int, take.id),
                        take_line_id=cast(int, take_line.id),
                        location_id=source.location_id,
                        quantity=source.quantity,
                        movement_id=cast(int, movement.id),
                    )
                )
        session.commit()
    except Exception:
        # Half a BOM taken and reported as success is worse than asking the user
        # to look again — a concurrent removal surfaces here as InsufficientStock.
        session.rollback()
        raise
    session.refresh(take)
    return take


def get_take(session: Session, take_id: int) -> BomTake:
    return require_entity(session, BomTake, take_id, "bom take")


def list_takes(session: Session, bom_id: int) -> list[BomTake]:
    """Every take of this BOM, newest first."""
    return list(
        session.exec(
            select(BomTake)
            .where(BomTake.bom_id == bom_id)
            .order_by(col(BomTake.id).desc())
        ).all()
    )


def take_lines(session: Session, take_id: int) -> list[BomTakeLine]:
    return list(
        session.exec(
            select(BomTakeLine)
            .where(BomTakeLine.take_id == take_id)
            .order_by(col(BomTakeLine.id))
        ).all()
    )


def take_allocations(session: Session, take_id: int) -> list[BomTakeAllocation]:
    return list(
        session.exec(
            select(BomTakeAllocation)
            .where(BomTakeAllocation.take_id == take_id)
            .order_by(col(BomTakeAllocation.id))
        ).all()
    )


def reverse_take(
    session: Session, take_id: int, *, reason: str, user_id: int
) -> BomTake:
    """Put everything back where it came from, saying why.

    The reason is required: a reversal that does not say what happened to the
    board is a hole in exactly the record this table exists to keep.
    """
    take = get_take(session, take_id)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("say why this take is being reversed")

    # Checked BEFORE the claim, for the reason plan_take checks before the take:
    # `add_stock` refuses a retired component, so hitting it mid-loop would roll
    # back the reversal — including the mark — and the message would name neither
    # the take nor the line. One retired part must not silently mean "the other
    # nine stay off the shelves and you cannot find out why".
    lines = {cast(int, ln.id): ln for ln in take_lines(session, take_id)}
    allocations = take_allocations(session, take_id)
    moved = {lines[a.take_line_id].component_id for a in allocations}
    live = components_by_id(session, moved)
    retired = sorted(
        {
            lines[a.take_line_id].references
            for a in allocations
            if (part := live.get(lines[a.take_line_id].component_id)) is None
            or part.deleted_at is not None
        }
    )
    if retired:
        raise ValidationError(
            "these lines were built from parts that are no longer in use, so "
            "their stock cannot be put back: " + ", ".join(retired)
        )

    now = datetime.now(UTC)
    # Claim the reversal atomically, the way finalize_invoice claims an invoice: a
    # double-clicked Undo must not return the same stock twice.
    claimed = cast(
        "CursorResult[object]",
        session.execute(
            update(BomTake)
            .where(
                col(BomTake.id) == take_id,
                col(BomTake.reversed_at).is_(None),
            )
            .values(reversed_at=now, reversed_by=user_id, reversal_reason=reason)
        ),
    )
    if claimed.rowcount != 1:
        raise ValidationError("this take has already been reversed")

    try:
        for allocation in allocations:
            line = lines[allocation.take_line_id]
            movement = ss.add_stock(
                session,
                component_id=line.component_id,
                location_id=allocation.location_id,
                quantity=allocation.quantity,
                user_id=user_id,
                reason=StockReason.CORRECTION,
                # Not PURCHASE (nothing was bought) and not MOVE (that is a
                # two-leg relocation, and one leg alone would not balance). What
                # it really was is in the note and the snapshot it links to.
                note=f"Reversed {take.name}: {reason}",
                commit=False,
            )
            allocation.reversal_movement_id = cast(int, movement.id)
            session.add(allocation)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(take)
    return take


def take_detail(session: Session, take_id: int) -> dict[str, object]:
    """The snapshot as the page and the API both want it.

    Location paths are resolved defensively: `delete_location` refuses only on
    non-zero stock, so the gathering tree is deletable the moment a take empties
    it, and a snapshot must survive that as "—" rather than a 500.
    """
    take = get_take(session, take_id)
    lines = take_lines(session, take_id)
    allocations = take_allocations(session, take_id)
    by_line: dict[int, list[dict[str, object]]] = {}
    for allocation in allocations:
        by_line.setdefault(allocation.take_line_id, []).append(
            {
                "location_id": allocation.location_id,
                "path": ls.path_or_dash(session, allocation.location_id),
                "quantity": allocation.quantity,
                "movement_id": allocation.movement_id,
                "reversal_movement_id": allocation.reversal_movement_id,
            }
        )
    return {
        "id": take.id,
        "bom_id": take.bom_id,
        "name": take.name,
        "boards": take.boards,
        "source_path": (
            ls.path_or_dash(session, take.source_location_id)
            if take.source_location_id is not None
            else "—"
        ),
        "created_at": take.created_at.isoformat(),
        "reversed_at": take.reversed_at.isoformat() if take.reversed_at else None,
        "reversal_reason": take.reversal_reason,
        "lines": [
            {
                "id": line.id,
                "references": line.references,
                "component_id": line.component_id,
                "requested": line.requested_quantity,
                "taken": line.taken_quantity,
                "shortfall": line.requested_quantity - line.taken_quantity,
                "sources": by_line.get(cast(int, line.id), []),
            }
            for line in lines
        ],
    }


def takes_by_movement(
    session: Session, movement_ids: Iterable[int]
) -> dict[int, BomTake]:
    """``{movement_id: take}`` for both legs — the take's and its reversal's.

    Batched, so rendering a component's movements costs one extra query however
    long the list is.
    """
    ids = list(movement_ids)
    if not ids:
        return {}
    rows = session.exec(
        select(BomTakeAllocation, BomTake)
        .where(BomTakeAllocation.take_id == BomTake.id)
        .where(
            col(BomTakeAllocation.movement_id).in_(ids)
            | col(BomTakeAllocation.reversal_movement_id).in_(ids)
        )
    ).all()
    found: dict[int, BomTake] = {}
    wanted = set(ids)
    for allocation, take in rows:
        if allocation.movement_id in wanted:
            found[allocation.movement_id] = take
        if allocation.reversal_movement_id in wanted:
            found[allocation.reversal_movement_id] = take
    return found

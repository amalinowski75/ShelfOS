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
from app.services import equivalence_service as es
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services._common import require_entity
from app.services.errors import ValidationError

# Why a line cannot be taken. Both are refusals of the whole run, not of the line:
# a take that quietly skipped a part would produce a board's worth of movements
# and no board.
BLOCKED_UNASSIGNED = "unassigned"
BLOCKED_RETIRED = "component_retired"

# SQLite allows 999 bound variables by default and `takes_by_movement` binds each
# id twice, so this is half of that with room to spare.
_MOVEMENT_CHUNK = 400


@dataclass
class PlannedSource:
    """One location a line's parts will come from, or could come from.

    Carries its own component: a line can be built from several entries of one
    part (D15), so "where from" is only half the answer — the dialog has to say
    WHICH of them a bin holds, and the question about an ambiguous shelf is asked
    per entry rather than per line.
    """

    component_id: int
    mpn: str | None
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


def _has_slot(
    slots: dict[int, list[ComponentLocation]], component_id: int, location_id: int
) -> bool:
    """Whether this part was ever stocked here — before other lines claimed it."""
    return any(
        slot.location_id == location_id for slot in slots.get(component_id, [])
    )


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
    choices: dict[tuple[int, int | None], int] | None = None,
) -> TakePlan:
    """Work out what this take would do. Reads only; writes nothing.

    ``overrides`` maps a BOM line id to the quantity the user typed instead of
    ``line.quantity * boards`` — parts get lost during assembly, so the number is
    editable. ``choices`` maps a (line id, component id) pair to the location the user
    picked when several outside the gathering branch hold THAT entry of the part —
    a line built from two entries can be asked twice. A ``None`` component is an
    answer from a page that did not name one: it is matched to whichever entry
    actually holds that bin (see below).
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
    # Every catalogue entry each assigned part is the same thing as (D15), and the
    # entries themselves — two queries for the whole plan rather than two per line.
    same_part = es.equivalent_ids_for(session, set(components))
    variants = components_by_id(
        session, {member for members in same_part.values() for member in members}
    )
    # Bins for every entry a line could be built from, not just the assigned one.
    slots = ss.slots_by_component(
        session,
        {
            c.id
            for c in (*components.values(), *variants.values())
            if c.deleted_at is None and c.id
        },
    )
    # What each slot has LEFT as the plan walks the lines. Two designator groups
    # can be assigned to the same component (the assignment is unique per
    # references, not per component), and two lines can now reach the same shelf
    # through DIFFERENT entries of one part. Without this each of them would plan
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

    def left_of(part_id: int, *, inside: bool) -> int:
        """What is still unspoken-for in this part's bins on one side of the tree.

        Read when the pass that uses it is about to run, never once for the line.
        That re-read is what keeps a 3-piece remnant ahead of a 50-piece bag when
        the same entry also had 500 gathered: ranked before the gathering pass, the
        reel is the fullest entry there is and would be reached last, leaving its
        remnant on the shelf for ever.

        The side matters to the FIRST pass, where a gathered bin must not be ranked
        by what its entry also holds out on a shelf. By the time the second runs,
        the gathering branch is empty — it only stops early when the line is
        already satisfied — so there the two readings agree.
        """
        return sum(
            unclaimed[(part_id, slot.location_id)]
            for slot in slots.get(part_id, [])
            if (slot.location_id in inside_ids) is inside
        )

    def emptiest_first(parts: list[Component], *, inside: bool) -> list[Component]:
        """The entries in the order to draw from them, emptiest leading.

        The id breaks a tie so the order cannot drift between runs.
        """
        return sorted(
            parts,
            key=lambda part: (
                left_of(cast(int, part.id), inside=inside),
                cast(int, part.id),
            ),
        )

    def bins_of(part: Component, *, inside: bool) -> list[PlannedSource]:
        """This part's bins on one side of the gathering branch, emptiest last.

        Built fresh for each pass, so `available` is what is left after the pass
        before it rather than what the shelf started the line with.
        """
        part_id = cast(int, part.id)
        found = [
            PlannedSource(
                component_id=part_id,
                mpn=part.mpn,
                location_id=slot.location_id,
                path=path_of(slot.location_id),
                available=unclaimed[(part_id, slot.location_id)],
                quantity=0,
                inside=slot.location_id in inside_ids,
            )
            for slot in slots.get(part_id, [])
            if unclaimed[(part_id, slot.location_id)] > 0
        ]
        return sorted(
            [source for source in found if source.inside is inside],
            key=_by_biggest_bin,
        )

    def claim(source: PlannedSource, amount: int) -> int:
        """Take ``amount`` from this slot and keep the running total honest."""
        source.quantity += amount
        unclaimed[(source.component_id, source.location_id)] -= amount
        return amount

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
        assigned_id = cast(int, component.id)

        # What this line may be built from: the part someone assigned, and every
        # live entry the catalogue calls the same part (D15). The assignment still
        # names one component — that is the decision — and this is only where the
        # parts may come from.
        #
        # Each pass orders these for itself, emptiest entry first: a part-used bag
        # and a loose remnant go before a sealed reel, which is what a person
        # reaching onto the shelf does — it closes out the awkward leftovers
        # instead of leaving a dozen bins with nine parts in them.
        #
        # No filter for a retired entry here, and none is needed: `slots` is built
        # from the live entries alone, and a part cannot be taken out of use while
        # its stock is on the shelf — so a retired one has no bin to offer either
        # way. A check here would imply a case that cannot arise.
        parts = [
            variant
            for member_id in same_part.get(assigned_id, [assigned_id])
            if (variant := variants.get(member_id)) is not None
        ] or [component]

        remaining = entry.requested
        # The gathering branch is EXHAUSTED before anything else is considered, and
        # across every entry of the part: what was put out for this board was put
        # out for it whatever index it carries. Spreading across the sub-containers
        # needs no question — they are all "the parts I gathered".
        for part in emptiest_first(parts, inside=True):
            for candidate in bins_of(part, inside=True):
                if remaining <= 0:
                    break
                entry.sources.append(candidate)
                remaining -= claim(candidate, min(remaining, candidate.available))
            if remaining <= 0:
                break

        # Then the ordinary shelves, one entry of the part at a time — ordered
        # again, and on what is left OUT HERE, because the pass above has just
        # changed both.
        for part in emptiest_first(parts, inside=False):
            if remaining <= 0:
                break
            outside = bins_of(part, inside=False)
            if not outside:
                continue
            part_id = cast(int, part.id)
            # Offered whenever there is a real choice to make, answered or not:
            # the answer has to be revisable, and a picker that vanishes the
            # moment it is used cannot be corrected without starting over.
            if len(outside) > 1:
                entry.candidates.extend(outside)
            chosen_id = choices.get((line_id, part_id))
            named_the_entry = chosen_id is not None
            if chosen_id is None:
                # An answer from a page old enough not to name an entry. Such a
                # page rendered every candidate into ONE picker, so the location it
                # sends can belong to a variant rather than to the assigned part —
                # reading it as the assigned part's would refuse a choice the page
                # itself offered. Match it to the entry that actually holds the bin.
                unnamed = choices.get((line_id, None))
                if unnamed is not None and _has_slot(slots, part_id, unnamed):
                    chosen_id = unnamed
            if chosen_id is not None:
                picked = next(
                    (c for c in outside if c.location_id == chosen_id), None
                )
                if (
                    named_the_entry
                    and picked is None
                    and not _has_slot(slots, part_id, chosen_id)
                ):
                    # The location never held this entry at all — a client sending
                    # something the plan never offered, not a person's answer. Only
                    # for an answer that NAMED the entry: an unnamed one is matched
                    # above or left alone, never turned into a refusal.
                    raise ValidationError(
                        f"'{line.references}' does not have stock at the location "
                        "chosen for it"
                    )
                if picked is not None:
                    # An explicit choice is honoured for that location ALONE —
                    # spilling the rest into the bins the user did not pick would
                    # answer a different question than the one they were asked.
                    entry.sources.append(picked)
                    remaining -= claim(picked, min(remaining, picked.available))
                # `picked is None` with a slot that exists means an earlier line
                # took the lot. That is a shortfall on this line, the same as any
                # other, and not a reason to refuse the whole run: the answer was
                # true when it was given.
            elif len(outside) == 1:
                only = outside[0]
                entry.sources.append(only)
                remaining -= claim(only, min(remaining, only.available))
            else:
                # Stop the line here rather than planning the next entry of the
                # part around an unanswered question: the run is blocked until it
                # is answered, and once it is, this bin may well cover the rest.
                entry.needs_choice = True
                break

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


def _snapshot_rows(
    line: PlannedLine,
) -> list[tuple[int, int, list[PlannedSource]]]:
    """Split one planned line into the snapshot rows it becomes.

    One row per ENTRY of the part the line actually drew from, all sharing the
    designator group — the table allows that, and it is the honest record: "R1,R2
    took 40 of the bulk bag and 60 off the reel" is two movements from two
    components, and one row could only name one of them.

    ``requested`` is split in the order the parts were drawn, each row asking for
    exactly what it gave, so the rows still sum to what the line wanted. What the
    shelves could not give is added to the row of the part the line is ASSIGNED
    to — the one someone chose — and gets a row of its own when that part gave
    nothing. A line that found nothing anywhere is a single row of 0 taken, which
    is what it was before a line could be built from more than one entry.
    """
    drawn: dict[int, list[PlannedSource]] = {}
    for source in line.sources:
        drawn.setdefault(source.component_id, []).append(source)

    outstanding = line.requested
    rows: list[tuple[int, int, list[PlannedSource]]] = []
    for part_id, sources in drawn.items():
        taken = sum(source.quantity for source in sources)
        asked = min(outstanding, taken)
        outstanding -= asked
        rows.append((part_id, asked, sources))

    if outstanding or not rows:
        assigned_id = cast(int, line.component_id)
        for index, (part_id, asked, sources) in enumerate(rows):
            if part_id == assigned_id:
                rows[index] = (part_id, asked + outstanding, sources)
                break
        else:
            rows.append((assigned_id, outstanding, []))
    return rows


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
    choices: dict[tuple[int, int | None], int] | None = None,
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
            for part_id, requested, sources in _snapshot_rows(line):
                take_line = BomTakeLine(
                    take_id=cast(int, take.id),
                    references=line.references,
                    component_id=part_id,
                    requested_quantity=requested,
                    taken_quantity=sum(s.quantity for s in sources),
                )
                session.add(take_line)
                session.flush()
                for source in sources:
                    movement = ss.remove_stock(
                        session,
                        component_id=part_id,
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

    Location paths are resolved defensively: `delete_location` refuses while
    this take stands, but once it is reversed the gathering tree it emptied can
    go, and the snapshot must survive that as "—" rather than a 500.
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
    found: dict[int, BomTake] = {}
    wanted = set(ids)
    # Every id is bound TWICE — once per arm of the OR — and the caller is the
    # component page, which passes an uncapped movement list. So chunk against
    # half the ceiling rather than the whole of it, or a part with a long history
    # takes the page down with "too many SQL variables".
    for start in range(0, len(ids), _MOVEMENT_CHUNK):
        chunk = ids[start : start + _MOVEMENT_CHUNK]
        rows = session.exec(
            select(BomTakeAllocation, BomTake)
            .where(BomTakeAllocation.take_id == BomTake.id)
            .where(
                col(BomTakeAllocation.movement_id).in_(chunk)
                | col(BomTakeAllocation.reversal_movement_id).in_(chunk)
            )
        ).all()
        for allocation, take in rows:
            if allocation.movement_id in wanted:
                found[allocation.movement_id] = take
            if allocation.reversal_movement_id in wanted:
                found[allocation.reversal_movement_id] = take
    return found

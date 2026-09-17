"""Saying that two catalogue entries are the same physical part.

The rules live here rather than in the routes because they are what makes a group
mean anything (see :mod:`app.models.equivalence` for why the fact is worth storing
at all):

* A component belongs to **at most one** group. "Is the same part as" is
  transitive, so joining a part to a group joins it to every member at once; the
  unique key on ``component_id`` makes a second membership impossible rather than
  merely discouraged.
* A group of fewer than two members is deleted. It says nothing, and left behind it
  would silently reappear as soon as someone added a part to the component it hangs
  off — a group they never created, with someone else's note attached.
* A part that has been taken out of use cannot be joined to one. It is out of use;
  a decision about what it is equivalent to is a decision about nothing.

Deliberately importing no other service. ``component_service`` cleans up
memberships when it hard-deletes a part, so the dependency has to run that way
round and not both.
"""

from __future__ import annotations

from typing import cast

from sqlmodel import Session, col, select

from app.models.component import Component
from app.models.equivalence import (
    ComponentEquivalenceGroup,
    ComponentEquivalenceMember,
)
from app.services._common import require_entity
from app.services.errors import ValidationError

_MAX_NOTES_LEN = 2000


def _clean_notes(notes: str | None) -> str | None:
    text = (notes or "").strip()
    if len(text) > _MAX_NOTES_LEN:
        raise ValidationError(f"the note is longer than {_MAX_NOTES_LEN} characters")
    return text or None


def _require_live(session: Session, component_id: int) -> Component:
    component = require_entity(session, Component, component_id, "component")
    if component.deleted_at is not None:
        raise ValidationError("that component is no longer in use")
    return component


def membership(
    session: Session, component_id: int
) -> ComponentEquivalenceMember | None:
    """This component's place in a group, or ``None`` if it is on its own."""
    return session.exec(
        select(ComponentEquivalenceMember).where(
            ComponentEquivalenceMember.component_id == component_id
        )
    ).first()


def memberships_for(
    session: Session, component_ids: set[int]
) -> dict[int, ComponentEquivalenceMember]:
    """Each of these components' membership, in one query rather than per row.

    For a list of candidates, where "is this one already spoken for?" has to be
    answered for every row at once. An ungrouped component is absent from the
    result, exactly as :func:`membership` returns ``None`` for one.
    """
    if not component_ids:
        return {}
    rows = session.exec(
        select(ComponentEquivalenceMember).where(
            col(ComponentEquivalenceMember.component_id).in_(component_ids)
        )
    ).all()
    return {member.component_id: member for member in rows}


def group_for(
    session: Session, component_id: int
) -> ComponentEquivalenceGroup | None:
    """The group this component is in, or ``None``."""
    member = membership(session, component_id)
    if member is None:
        return None
    return session.get(ComponentEquivalenceGroup, member.group_id)


def members_of(session: Session, group_id: int) -> list[ComponentEquivalenceMember]:
    """A group's memberships, oldest first — the order they were decided in."""
    return list(
        session.exec(
            select(ComponentEquivalenceMember)
            .where(ComponentEquivalenceMember.group_id == group_id)
            .order_by(col(ComponentEquivalenceMember.id))
        ).all()
    )


def equivalent_ids(session: Session, component_id: int) -> list[int]:
    """Every component that is this same part, this one included.

    Always returns at least the component itself, so a caller summing stock over
    "the parts this line can use" needs no special case for an ungrouped part —
    which is the overwhelmingly common one.

    Includes members taken out of use, unflagged. They hold no stock — a part
    cannot be retired while its stock is on the shelf — so a sum over this list is
    right as it stands, but a caller that offers these ids as somewhere to TAKE
    parts from has to check ``deleted_at`` itself.
    """
    member = membership(session, component_id)
    if member is None:
        return [component_id]
    return [m.component_id for m in members_of(session, member.group_id)]


def equivalent_ids_for(
    session: Session, component_ids: set[int]
) -> dict[int, list[int]]:
    """:func:`equivalent_ids` for several components at once, in two queries.

    For the BOM report, which asks the question once per assigned line and would
    otherwise walk the membership table twice per line. Every component asked
    about appears in the result, an ungrouped one mapped to itself alone, so the
    caller reads it the same way whatever the answer.

    Includes retired members, with the warning :func:`equivalent_ids` gives.
    """
    if not component_ids:
        return {}
    mine = memberships_for(session, component_ids)
    group_ids = {member.group_id for member in mine.values()}
    by_group: dict[int, list[int]] = {}
    if group_ids:
        rows = session.exec(
            select(ComponentEquivalenceMember)
            .where(col(ComponentEquivalenceMember.group_id).in_(group_ids))
            .order_by(col(ComponentEquivalenceMember.id))
        ).all()
        for row in rows:
            by_group.setdefault(row.group_id, []).append(row.component_id)
    return {
        component_id: (
            by_group[mine[component_id].group_id]
            if component_id in mine
            else [component_id]
        )
        for component_id in component_ids
    }


_CANDIDATE_LIMIT = 25
# LIKE's own wildcards, escaped so a typed "%" searches for a per-cent sign rather
# than matching the whole catalogue.
_LIKE_SPECIALS = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def search_candidates(
    session: Session, component_id: int, query: str, *, limit: int = _CANDIDATE_LIMIT
) -> list[Component]:
    """Live parts whose MPN or maker contains ``query``, as variants to pick from.

    A substring match, not the exact-MPN lookup the rest of the app uses, because
    the whole reason this feature exists is that the numbers differ: "AO3400A" has
    to find "AO3400A-TR". The component itself and everything already in its group
    are left out — they are not choices — while a part belonging to some OTHER
    group is returned, so the panel can show it greyed with a reason rather than
    hiding it and leaving the user to wonder where it went.
    """
    text = (query or "").strip()
    if not text:
        return []
    pattern = f"%{text.translate(_LIKE_SPECIALS)}%"
    exclude = set(equivalent_ids(session, component_id))
    rows = session.exec(
        select(Component)
        .where(col(Component.deleted_at).is_(None))
        .where(col(Component.id).not_in(exclude))
        .where(
            col(Component.mpn).ilike(pattern, escape="\\")
            | col(Component.manufacturer).ilike(pattern, escape="\\")
        )
        .order_by(col(Component.mpn), col(Component.id))
        .limit(limit)
    ).all()
    return list(rows)


def link_components(
    session: Session,
    component_id: int,
    other_id: int,
    *,
    user_id: int,
    notes: str | None = None,
) -> ComponentEquivalenceGroup:
    """Record that these two entries are the same part. Returns their group.

    Joins ``other_id`` to whatever group ``component_id`` is already in, creating
    one if neither is grouped yet. Idempotent when both are already together, so a
    double-clicked button is not an error.

    Refused when the other part already belongs to a DIFFERENT group. Merging two
    groups is a much larger claim than adding one part — it says every member of
    one equals every member of the other — and it is not a claim to make on
    someone's behalf while they thought they were adding a single variant.
    """
    if component_id == other_id:
        raise ValidationError("a component is already the same part as itself")
    _require_live(session, component_id)
    other = _require_live(session, other_id)

    mine = membership(session, component_id)
    theirs = membership(session, other_id)
    if theirs is not None and (mine is None or theirs.group_id != mine.group_id):
        raise ValidationError(
            f"'{other.mpn or f'#{other_id}'}' already belongs to another group of "
            "equivalent parts; remove it from that one first"
        )
    if mine is not None and theirs is not None:
        group = session.get(ComponentEquivalenceGroup, mine.group_id)
        if group is None:  # pragma: no cover - the FK makes this unreachable
            raise ValidationError("that group of equivalent parts no longer exists")
        return group  # already said, by someone or by an earlier click

    if mine is None:
        group = ComponentEquivalenceGroup(
            notes=_clean_notes(notes), created_by=user_id
        )
        session.add(group)
        session.flush()  # its id, for the memberships below
        session.add(
            ComponentEquivalenceMember(
                group_id=cast(int, group.id), component_id=component_id
            )
        )
    else:
        group = session.get(ComponentEquivalenceGroup, mine.group_id)
        if group is None:  # pragma: no cover - the FK makes this unreachable
            raise ValidationError("that group of equivalent parts no longer exists")
        # A note given while adding to an existing group fills a blank one rather
        # than overwriting what the person who created it wrote.
        if group.notes is None:
            group.notes = _clean_notes(notes)
            session.add(group)

    session.add(
        ComponentEquivalenceMember(group_id=cast(int, group.id), component_id=other_id)
    )
    session.commit()
    session.refresh(group)
    return group


def unlink_component(session: Session, component_id: int) -> None:
    """Take this component out of its group."""
    member = membership(session, component_id)
    if member is None:
        raise ValidationError("that component is not grouped with any other part")
    _drop_membership(session, member)
    session.commit()


def _drop_membership(
    session: Session, member: ComponentEquivalenceMember
) -> None:
    """Remove one membership, and the group if it leaves fewer than two. No commit.

    Removing the second-to-last member takes the group with it: one part is not
    equivalent to anything, and an empty shell left behind would adopt the next
    part added to that component as though the group had always been there — with
    someone else's note already attached to it.
    """
    group_id = member.group_id
    session.delete(member)
    session.flush()
    remaining = members_of(session, group_id)
    if len(remaining) < 2:
        for leftover in remaining:
            session.delete(leftover)
        group = session.get(ComponentEquivalenceGroup, group_id)
        if group is not None:
            session.delete(group)


def set_notes(
    session: Session, group_id: int, *, notes: str | None
) -> ComponentEquivalenceGroup:
    """Rewrite why these parts are one part. Blank clears it."""
    group = require_entity(
        session, ComponentEquivalenceGroup, group_id, "equivalence group"
    )
    group.notes = _clean_notes(notes)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


def delete_members_for(session: Session, component_id: int) -> None:
    """Drop a component's membership without committing, for a hard delete.

    Uncommitted because it runs inside the caller's transaction: a hard delete that
    fails half way must not leave the part out of its group. Unlike
    :func:`unlink_component` this does not raise for an ungrouped part — the caller
    is deleting it either way.
    """
    member = membership(session, component_id)
    if member is not None:
        _drop_membership(session, member)

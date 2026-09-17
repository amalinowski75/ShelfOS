"""Grouping catalogue entries that are one physical part.

Nested under ``/api/components/{id}`` rather than given a namespace of its own: a
group is always reached from one of its parts, and there is no screen that asks
"show me all the groups". Mounted with the other protected routers, so a read-only
account can see a group but not change one.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    EquivalenceCandidateRead,
    EquivalenceLinkWrite,
    EquivalenceMemberRead,
    EquivalenceNotesWrite,
    EquivalenceRead,
)
from app.auth.deps import current_user_id
from app.models.component import Component
from app.services import equivalence_service as svc
from app.services import stock_service as ss
from app.services._common import require_entity
from app.services.errors import ValidationError

router = APIRouter(prefix="/api/components", tags=["equivalence"])


def _read(session: Session, component_id: int) -> EquivalenceRead:
    """The group as the component page reads it, ungrouped parts included."""
    require_entity(session, Component, component_id, "component")
    group = svc.group_for(session, component_id)
    members = [
        require_entity(session, Component, member_id, "component")
        for member_id in svc.equivalent_ids(session, component_id)
    ]
    rows = [
        EquivalenceMemberRead(
            component_id=cast(int, member.id),
            mpn=member.mpn,
            manufacturer=member.manufacturer,
            package=member.package,
            stock=ss.total_quantity(session, cast(int, member.id)),
            deleted=member.deleted_at is not None,
        )
        for member in members
    ]
    return EquivalenceRead(
        group_id=group.id if group else None,
        notes=group.notes if group else None,
        # Every member, with no exception for a retired one — it cannot hold any.
        # Taking a part out of use is refused while its stock is on the shelf, so a
        # deleted entry contributes zero by construction, and a filter here would
        # imply a case that cannot arise.
        total_stock=sum(row.stock for row in rows),
        members=rows,
    )


@router.get("/{component_id}/equivalents", response_model=EquivalenceRead)
def get_equivalents(
    component_id: int, session: Session = Depends(get_session)
) -> EquivalenceRead:
    """Which catalogue entries are the same part as this one."""
    return _read(session, component_id)


@router.get(
    "/{component_id}/equivalents/candidates",
    response_model=list[EquivalenceCandidateRead],
)
def search_equivalent_candidates(
    component_id: int,
    q: str = Query("", description="Part of an MPN or manufacturer name"),
    session: Session = Depends(get_session),
) -> list[EquivalenceCandidateRead]:
    """Parts that could be the same as this one, by substring of MPN or maker."""
    require_entity(session, Component, component_id, "component")
    return [
        EquivalenceCandidateRead(
            component_id=cast(int, candidate.id),
            mpn=candidate.mpn,
            manufacturer=candidate.manufacturer,
            package=candidate.package,
            stock=ss.total_quantity(session, cast(int, candidate.id)),
            grouped=svc.membership(session, cast(int, candidate.id)) is not None,
        )
        for candidate in svc.search_candidates(session, component_id, q)
    ]


@router.post(
    "/{component_id}/equivalents",
    response_model=EquivalenceRead,
    status_code=status.HTTP_201_CREATED,
)
def add_equivalent(
    component_id: int,
    payload: EquivalenceLinkWrite,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> EquivalenceRead:
    """Say that another entry is the same part as this one (writers).

    Returns the whole group rather than the one row added: the panel redraws from
    this, and adding a part to an existing group changes what every other row's
    total means.
    """
    svc.link_components(
        session,
        component_id,
        payload.component_id,
        user_id=user_id,
        notes=payload.notes,
    )
    return _read(session, component_id)


@router.put("/{component_id}/equivalents/notes", response_model=EquivalenceRead)
def set_equivalence_notes(
    component_id: int,
    payload: EquivalenceNotesWrite,
    session: Session = Depends(get_session),
) -> EquivalenceRead:
    """Rewrite why these entries are one part (writers). Blank clears it."""
    group = svc.group_for(session, component_id)
    if group is None or group.id is None:
        raise ValidationError("that component is not grouped with any other part")
    svc.set_notes(session, group.id, notes=payload.notes)
    return _read(session, component_id)


@router.delete(
    "/{component_id}/equivalents/{other_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_equivalent(
    component_id: int, other_id: int, session: Session = Depends(get_session)
) -> None:
    """Take one entry out of this component's group (writers).

    Addressed through the component whose page is open, and checked against it:
    ``other_id`` has to actually be in that group, so a stale panel cannot dissolve
    a group somewhere else in the catalogue.
    """
    if other_id not in svc.equivalent_ids(session, component_id):
        raise ValidationError("that component is not in this group of equivalent parts")
    svc.unlink_component(session, other_id)

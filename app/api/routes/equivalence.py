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
from app.services import component_service as cs
from app.services import equivalence_service as svc
from app.services import stock_service as ss
from app.services._common import refuse_if_deleted, require_entity
from app.services.errors import ValidationError

router = APIRouter(prefix="/api/components", tags=["equivalence"])


def _require_live_page(session: Session, component_id: int) -> Component:
    """The component the request was addressed through, refused if it is retired.

    Every write here is a decision about a part that is in use, and the component
    page hides its write controls once the part is taken out of use. Without this
    the panel is the one place that stayed writable: its Remove button would
    dissolve a group from a page the rest of the app treats as read-only, and the
    membership a restore was supposed to bring back would already be gone.

    The check is on the page's component, never on the other one: dropping a
    RETIRED variant out of a live part's group is exactly what someone looking at
    the live part should be able to do.
    """
    component = require_entity(session, Component, component_id, "component")
    refuse_if_deleted(component, component.mpn or f"component #{component_id}")
    return component


def _read(session: Session, component_id: int) -> EquivalenceRead:
    """The group as the component page reads it, ungrouped parts included."""
    require_entity(session, Component, component_id, "component")
    group = svc.group_for(session, component_id)
    member_ids = svc.equivalent_ids(session, component_id)
    # Three queries for the whole panel, whatever its size: the memberships, the
    # components, the stock. One `session.get` and one `total_quantity` per member
    # is the shape that quietly turns a five-variant group into a dozen round
    # trips — and the candidate list below has twenty-five rows.
    components = cs.components_by_id(session, set(member_ids))
    stock = ss.total_quantities_for(session, set(member_ids))
    rows = [
        EquivalenceMemberRead(
            component_id=member_id,
            mpn=components[member_id].mpn,
            manufacturer=components[member_id].manufacturer,
            package=components[member_id].package,
            stock=stock.get(member_id, 0),
            deleted=components[member_id].deleted_at is not None,
        )
        for member_id in member_ids
        if member_id in components
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
    candidates = svc.search_candidates(session, component_id, q)
    # Three queries for the whole list, not two per row. This runs on every pause
    # in typing, so a per-row stock aggregate and a per-row membership lookup would
    # put fifty round trips behind every burst of keystrokes.
    ids = {cast(int, candidate.id) for candidate in candidates}
    stock = ss.total_quantities_for(session, ids)
    grouped = svc.memberships_for(session, ids)
    return [
        EquivalenceCandidateRead(
            component_id=cast(int, candidate.id),
            mpn=candidate.mpn,
            manufacturer=candidate.manufacturer,
            package=candidate.package,
            stock=stock.get(cast(int, candidate.id), 0),
            grouped=cast(int, candidate.id) in grouped,
        )
        for candidate in candidates
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
    _require_live_page(session, component_id)
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
    _require_live_page(session, component_id)
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
    _require_live_page(session, component_id)
    if other_id not in svc.equivalent_ids(session, component_id):
        raise ValidationError(
            "that component is not in this group of equivalent parts"
        )
    svc.unlink_component(session, other_id)

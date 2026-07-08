"""Storage location endpoints (spec §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import LocationCreate
from app.models.location import Location
from app.services import location_service as ls

router = APIRouter(prefix="/api/locations", tags=["locations"])


@router.post("", response_model=Location, status_code=status.HTTP_201_CREATED)
def create_location(
    payload: LocationCreate, session: Session = Depends(get_session)
) -> Location:
    return ls.create_location(
        session, type=payload.type, name=payload.name, parent_id=payload.parent_id
    )


@router.get("", response_model=list[Location])
def list_children(
    parent_id: int | None = None, session: Session = Depends(get_session)
) -> list[Location]:
    """List direct children of a location (or root locations when omitted)."""
    return ls.get_children(session, parent_id)


@router.get("/{location_id}/path", response_model=list[Location])
def get_path(
    location_id: int, session: Session = Depends(get_session)
) -> list[Location]:
    return ls.get_path(session, location_id)

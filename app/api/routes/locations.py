"""Storage location endpoints (spec §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    LocationBulkCreate,
    LocationBulkResult,
    LocationCreate,
    LocationUpdate,
)
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


@router.post(
    "/bulk", response_model=LocationBulkResult, status_code=status.HTTP_201_CREATED
)
def bulk_generate(
    payload: LocationBulkCreate,
    response: Response,
    session: Session = Depends(get_session),
) -> LocationBulkResult:
    """Generate a whole hierarchy (rack → shelves → …); dry_run previews it."""
    result = ls.generate_locations(
        session,
        parent_id=payload.parent_id,
        levels=[
            ls.BulkLevel(type=lv.type, count=lv.count, name_pattern=lv.name_pattern)
            for lv in payload.levels
        ],
        dry_run=payload.dry_run,
    )
    if payload.dry_run:
        response.status_code = status.HTTP_200_OK
    return LocationBulkResult(
        total=result.total,
        created=len(result.created_ids),
        sample_paths=result.sample_paths,
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


@router.patch("/{location_id}", response_model=Location)
def update_location(
    location_id: int,
    payload: LocationUpdate,
    session: Session = Depends(get_session),
) -> Location:
    """Rename, retype and/or move a location; omitted fields stay unchanged."""
    return ls.update_location(
        session, location_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_location(
    location_id: int,
    recursive: bool = False,
    session: Session = Depends(get_session),
) -> None:
    """Delete an empty location; ``recursive`` takes its whole stock-free branch."""
    ls.delete_location(session, location_id, recursive=recursive)

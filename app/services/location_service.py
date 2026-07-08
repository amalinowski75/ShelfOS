"""Storage-location hierarchy business logic (spec §7).

Locations form a tree (room → rack → shelf → …). This service manages creation
and traversal while preventing cycles.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.enums import LocationType
from app.models.location import Location
from app.services._common import require_entity
from app.services.errors import ValidationError


def create_location(
    session: Session,
    *,
    type: LocationType,
    name: str,
    parent_id: int | None = None,
) -> Location:
    """Create a location, optionally nested under a parent location."""
    if not name.strip():
        raise ValidationError("location name must not be empty")
    if parent_id is not None:
        require_entity(session, Location, parent_id, "location")
    location = Location(type=type, name=name, parent_id=parent_id)
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


def get_path(session: Session, location_id: int) -> list[Location]:
    """Return the location chain from root to the given location (inclusive)."""
    chain: list[Location] = []
    seen: set[int] = set()
    current: int | None = location_id
    while current is not None:
        if current in seen:
            raise ValidationError(f"cycle detected in location hierarchy at {current}")
        seen.add(current)
        location = require_entity(session, Location, current, "location")
        chain.append(location)
        current = location.parent_id
    chain.reverse()
    return chain


def get_children(session: Session, parent_id: int | None) -> list[Location]:
    """Return the direct children of a location (or roots when ``None``)."""
    return list(
        session.exec(
            select(Location)
            .where(Location.parent_id == parent_id)
            .order_by(Location.name)
        ).all()
    )


def format_path(session: Session, location_id: int) -> str:
    """Return a human-readable path such as ``"Lab / Rack A / Shelf 1"``."""
    return " / ".join(loc.name for loc in get_path(session, location_id))

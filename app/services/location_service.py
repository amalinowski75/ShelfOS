"""Storage-location hierarchy business logic (spec §7).

Locations form a tree (room → rack → shelf → …). This service manages creation
and traversal while preventing cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from sqlmodel import Session, col, select

from app.models.enums import LocationType
from app.models.location import Location
from app.services._common import require_entity
from app.services.errors import ValidationError

# A physical storage hierarchy is never remotely this deep; the cap keeps the
# recursive tree render (and any client walk) from a pathological chain.
_MAX_DEPTH = 32


@dataclass
class LocationNode:
    """A location plus its full path and nested children (for tree rendering)."""

    location: Location
    path: str
    children: list[LocationNode] = field(default_factory=list)


def location_tree(session: Session) -> list[LocationNode]:
    """Return the location forest as nested nodes, each with its full path.

    Built from a single ``list_all`` query; roots first, children sorted by name.
    ``build`` descends only from the None-rooted forest, so a cyclic component
    (every node non-null parent) is never reached — no infinite recursion —
    while ``path_of`` walks up with a visited-set guard (mirroring get_path).
    """
    locations = list_all(session)
    by_id = {loc.id: loc for loc in locations}
    children: dict[int | None, list[Location]] = {}
    for loc in locations:
        children.setdefault(loc.parent_id, []).append(loc)
    for group in children.values():
        group.sort(key=lambda loc: loc.name.lower())

    def path_of(loc: Location) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        current: Location | None = loc
        while current is not None and current.id not in seen:
            seen.add(cast(int, current.id))
            parts.append(current.name)
            current = by_id.get(current.parent_id)
        return " / ".join(reversed(parts))

    def build(parent_id: int | None) -> list[LocationNode]:
        return [
            LocationNode(
                location=loc,
                path=path_of(loc),
                children=build(cast(int, loc.id)),
            )
            for loc in children.get(parent_id, [])
        ]

    return build(None)


def flatten_tree(nodes: list[LocationNode]) -> list[LocationNode]:
    """Pre-order flattening of a location forest (parents before children)."""
    flat: list[LocationNode] = []
    for node in nodes:
        flat.append(node)
        flat.extend(flatten_tree(node.children))
    return flat


def list_all(session: Session) -> list[Location]:
    """Return every location ordered by name (for selection dropdowns)."""
    return list(session.exec(select(Location).order_by(col(Location.name))).all())


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
        if len(get_path(session, parent_id)) >= _MAX_DEPTH:
            raise ValidationError(
                f"location hierarchy may not be deeper than {_MAX_DEPTH} levels"
            )
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

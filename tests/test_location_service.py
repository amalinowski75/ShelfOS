"""Tests for location_service: hierarchy creation and traversal."""

from __future__ import annotations

import pytest
from app.models.enums import LocationType
from app.services import location_service as ls
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session


def test_create_nested_locations_and_path(session: Session) -> None:
    room = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=room.id
    )
    shelf = ls.create_location(
        session, type=LocationType.SHELF, name="Shelf 1", parent_id=rack.id
    )

    assert ls.format_path(session, shelf.id) == "Lab / Rack A / Shelf 1"
    assert [loc.id for loc in ls.get_path(session, shelf.id)] == [
        room.id,
        rack.id,
        shelf.id,
    ]


def test_create_location_rejects_empty_name(session: Session) -> None:
    with pytest.raises(ValidationError):
        ls.create_location(session, type=LocationType.ROOM, name="")


def test_create_location_rejects_unknown_parent(session: Session) -> None:
    with pytest.raises(NotFoundError):
        ls.create_location(session, type=LocationType.RACK, name="Rack", parent_id=123)


def test_get_children(session: Session) -> None:
    room = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    ls.create_location(session, type=LocationType.RACK, name="B", parent_id=room.id)
    ls.create_location(session, type=LocationType.RACK, name="A", parent_id=room.id)

    children = ls.get_children(session, room.id)
    # Ordered by name.
    assert [c.name for c in children] == ["A", "B"]
    assert ls.get_children(session, None) == [room]

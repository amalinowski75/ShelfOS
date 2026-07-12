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


def test_create_location_caps_nesting_depth(session: Session) -> None:
    """A pathologically deep chain is rejected so the tree render can't blow up."""
    parent_id: int | None = None
    for i in range(ls._MAX_DEPTH):
        loc = ls.create_location(
            session, type=LocationType.BOX, name=f"L{i}", parent_id=parent_id
        )
        parent_id = loc.id
    with pytest.raises(ValidationError, match="deeper than"):
        ls.create_location(
            session, type=LocationType.BOX, name="too deep", parent_id=parent_id
        )


def test_location_tree_structure_and_paths(session: Session) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )
    ls.create_location(
        session, type=LocationType.SHELF, name="Shelf 1", parent_id=rack.id
    )
    ls.create_location(session, type=LocationType.ROOM, name="Bench")  # second root

    tree = ls.location_tree(session)
    # Roots sorted by name: Bench before Lab.
    assert [n.location.name for n in tree] == ["Bench", "Lab"]
    lab_node = tree[1]
    assert lab_node.path == "Lab"
    assert lab_node.children[0].path == "Lab / Rack A"
    assert lab_node.children[0].children[0].path == "Lab / Rack A / Shelf 1"

    # flatten_tree is pre-order (parents before their children).
    assert [n.location.name for n in ls.flatten_tree(tree)] == [
        "Bench",
        "Lab",
        "Rack A",
        "Shelf 1",
    ]

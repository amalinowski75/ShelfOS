"""Tests for location_service: hierarchy creation, editing and traversal."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from app.models.enums import LocationType, StockReason
from app.models.invoice import InvoiceLine
from app.models.location import ComponentLocation
from app.models.stock import StockMovement
from app.services import component_service as cs
from app.services import location_service as ls
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session, select


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


def test_generate_locations_builds_the_whole_hierarchy(session: Session) -> None:
    rack = ls.create_location(session, type=LocationType.RACK, name="Cabinet")
    result = ls.generate_locations(
        session,
        parent_id=rack.id,
        levels=[
            ls.BulkLevel(type=LocationType.SHELF, count=2),
            ls.BulkLevel(type=LocationType.DRAWER, count=3, name_pattern="D{n}"),
        ],
    )

    # 2 shelves + 2×3 drawers.
    assert result.total == 8
    assert len(result.created_ids) == 8
    shelves = ls.get_children(session, rack.id)
    assert [loc.name for loc in shelves] == ["Shelf 1", "Shelf 2"]
    assert all(loc.type == LocationType.SHELF for loc in shelves)
    for shelf in shelves:
        drawers = ls.get_children(session, shelf.id)
        assert [loc.name for loc in drawers] == ["D1", "D2", "D3"]
    assert result.sample_paths[0] == "Cabinet / Shelf 1 / D1"


def test_generate_locations_zero_pads_to_the_count_width(session: Session) -> None:
    """Sibling sort is name.lower(), so numbering must be zero-padded."""
    result = ls.generate_locations(
        session,
        parent_id=None,
        levels=[ls.BulkLevel(type=LocationType.DRAWER, count=12)],
    )
    names = [loc.name for loc in ls.get_children(session, None)]
    assert names[:3] == ["Drawer 01", "Drawer 02", "Drawer 03"]
    assert names[-1] == "Drawer 12"
    assert result.total == 12


def test_generate_locations_dry_run_touches_nothing(session: Session) -> None:
    result = ls.generate_locations(
        session,
        parent_id=None,
        levels=[
            ls.BulkLevel(type=LocationType.SHELF, count=2),
            ls.BulkLevel(type=LocationType.DRAWER, count=2),
        ],
        dry_run=True,
    )
    assert result.total == 6
    assert result.created_ids == []
    assert result.sample_paths[0] == "Shelf 1 / Drawer 1"
    assert ls.list_all(session) == []


def test_generate_locations_rejects_conflicts_caps_and_bad_patterns(
    session: Session,
) -> None:
    ls.create_location(session, type=LocationType.SHELF, name="shelf 1")

    # Case-insensitive clash with an existing sibling: nothing is created.
    with pytest.raises(ValidationError, match="already exist"):
        ls.generate_locations(
            session,
            parent_id=None,
            levels=[ls.BulkLevel(type=LocationType.SHELF, count=2)],
        )
    assert len(ls.list_all(session)) == 1

    # A multi-child pattern must carry {n}, or every sibling would collide.
    with pytest.raises(ValidationError, match="placeholder"):
        ls.generate_locations(
            session,
            parent_id=None,
            levels=[ls.BulkLevel(type=LocationType.BOX, count=2, name_pattern="Box")],
        )

    # The node cap counts every level, not just the last one.
    with pytest.raises(ValidationError, match="limit per request"):
        ls.generate_locations(
            session,
            parent_id=None,
            levels=[
                ls.BulkLevel(type=LocationType.SHELF, count=30),
                ls.BulkLevel(type=LocationType.DRAWER, count=30),
            ],
        )

    with pytest.raises(ValidationError, match="at least one level"):
        ls.generate_locations(session, parent_id=None, levels=[])


def test_generate_locations_respects_depth_cap(session: Session) -> None:
    parent_id: int | None = None
    for i in range(ls._MAX_DEPTH - 1):
        loc = ls.create_location(
            session, type=LocationType.BOX, name=f"L{i}", parent_id=parent_id
        )
        parent_id = loc.id
    with pytest.raises(ValidationError, match="deeper than"):
        ls.generate_locations(
            session,
            parent_id=parent_id,
            levels=[
                ls.BulkLevel(type=LocationType.BOX, count=1),
                ls.BulkLevel(type=LocationType.BOX, count=1),
            ],
        )
    # A single level still fits (depth 31 + 1 = 32).
    result = ls.generate_locations(
        session,
        parent_id=parent_id,
        levels=[ls.BulkLevel(type=LocationType.BOX, count=1)],
    )
    assert result.total == 1


def test_update_location_renames_retypes_and_moves(session: Session) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    bench = ls.create_location(session, type=LocationType.ROOM, name="Bench")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )

    updated = ls.update_location(
        session, rack.id, name="Shelf X", type=LocationType.SHELF, parent_id=bench.id
    , user_id=1)
    assert (updated.name, updated.type, updated.parent_id) == (
        "Shelf X",
        LocationType.SHELF,
        bench.id,
    )
    assert ls.format_path(session, rack.id) == "Bench / Shelf X"

    # An omitted field stays untouched; parent_id=None moves to the top level.
    moved = ls.update_location(session, rack.id, parent_id=None, user_id=1)
    assert moved.name == "Shelf X"
    assert moved.parent_id is None


def test_update_location_rejects_empty_name_and_unknowns(session: Session) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    with pytest.raises(ValidationError):
        ls.update_location(session, lab.id, name="  ", user_id=1)
    with pytest.raises(ValidationError):
        ls.update_location(session, lab.id, name=None, user_id=1)
    with pytest.raises(NotFoundError):
        ls.update_location(session, 999, name="X", user_id=1)
    with pytest.raises(NotFoundError):
        ls.update_location(session, lab.id, parent_id=999, user_id=1)


def test_update_location_rejects_cycles(session: Session) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )
    shelf = ls.create_location(
        session, type=LocationType.SHELF, name="Shelf 1", parent_id=rack.id
    )

    with pytest.raises(ValidationError, match="under itself"):
        ls.update_location(session, lab.id, parent_id=lab.id, user_id=1)
    with pytest.raises(ValidationError, match="under itself"):
        ls.update_location(session, lab.id, parent_id=shelf.id, user_id=1)
    # The tree is untouched after the rejected moves.
    assert ls.format_path(session, shelf.id) == "Lab / Rack A / Shelf 1"


def test_update_location_move_respects_depth_cap(session: Session) -> None:
    """Moving a subtree counts its own height against the depth cap."""
    parent_id: int | None = None
    chain: list[int] = []
    for i in range(ls._MAX_DEPTH - 1):
        loc = ls.create_location(
            session, type=LocationType.BOX, name=f"L{i}", parent_id=parent_id
        )
        parent_id = loc.id
        chain.append(loc.id)

    # A two-level subtree: depth 31 + height 2 = 33 > 32 → rejected…
    top = ls.create_location(session, type=LocationType.BOX, name="top")
    ls.create_location(session, type=LocationType.BOX, name="kid", parent_id=top.id)
    with pytest.raises(ValidationError, match="deeper than"):
        ls.update_location(session, top.id, parent_id=chain[-1], user_id=1)
    # …but fits one level higher (depth 30 + height 2 = 32).
    ls.update_location(session, top.id, parent_id=chain[-2], user_id=1)
    assert ls.get_path(session, top.id)[-2].id == chain[-2]


def test_delete_location_blocked_by_children_and_stock(session: Session) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    drawer = ls.create_location(
        session, type=LocationType.DRAWER, name="D1", parent_id=lab.id
    )
    with pytest.raises(ValidationError, match="child locations"):
        ls.delete_location(session, lab.id, user_id=1)

    session.add(ComponentLocation(component_id=1, location_id=drawer.id, quantity=5))
    session.commit()
    with pytest.raises(ValidationError, match="holds stock"):
        ls.delete_location(session, drawer.id, user_id=1)

    with pytest.raises(NotFoundError):
        ls.delete_location(session, 999, user_id=1)


def test_delete_location_recursive_takes_the_whole_branch(session: Session) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )
    drawer = ls.create_location(
        session, type=LocationType.DRAWER, name="D1", parent_id=rack.id
    )
    bench = ls.create_location(session, type=LocationType.ROOM, name="Bench")
    # Cache and invoice references deep in the branch are cleaned up too.
    session.add(ComponentLocation(component_id=1, location_id=drawer.id, quantity=0))
    session.add(
        InvoiceLine(
            invoice_id=1,
            component_id=1,
            quantity=1,
            unit_price=Decimal("1.00"),
            total_price=Decimal("1.00"),
            location_id=drawer.id,
        )
    )
    session.commit()

    assert ls.delete_location(session, lab.id, recursive=True, user_id=1) == 3
    assert [loc.name for loc in ls.list_all(session)] == ["Bench"]
    assert session.exec(select(ComponentLocation)).all() == []
    assert session.exec(select(InvoiceLine)).one().location_id is None
    assert bench.id is not None  # the sibling root is untouched


def test_delete_location_recursive_blocked_by_stock_anywhere(
    session: Session,
) -> None:
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )
    drawer = ls.create_location(
        session, type=LocationType.DRAWER, name="D1", parent_id=rack.id
    )
    session.add(ComponentLocation(component_id=1, location_id=drawer.id, quantity=3))
    session.commit()

    # The error names WHERE the stock sits, and nothing was deleted.
    with pytest.raises(ValidationError, match="Lab / Rack A / D1"):
        ls.delete_location(session, lab.id, recursive=True, user_id=1)
    assert len(ls.list_all(session)) == 3


def test_delete_location_detaches_a_staged_import_line(session: Session) -> None:
    """A staged row points at a location too, and finalize re-checks that id.

    Left dangling, the invoice became un-finalizable with a bare "location N not
    found" — an error naming nothing the user could act on. Cleared, finalize
    asks for the location it is actually missing.
    """
    from app.models.invoice import InvoiceImportLine
    from app.services import invoice_service as inv

    drawer = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    invoice = inv.create_invoice(
        session,
        supplier="TME",
        invoice_number="FV-1",
        invoice_date=date(2026, 7, 8),
        currency="PLN",
    )
    ctype = cs.create_type(session, "resistor")
    session.add(
        InvoiceImportLine(
            invoice_id=invoice.id,
            line_no=1,
            mpn="R1",
            quantity=1,
            unit_price=Decimal("1.00"),
            shop_key="tme",
            type_id=ctype.id,
            location_id=drawer.id,
            reason="",
        )
    )
    session.commit()

    ls.delete_location(session, drawer.id, user_id=1)

    staged = session.exec(select(InvoiceImportLine)).one()
    assert staged.location_id is None
    with pytest.raises(ValidationError, match="must have a location"):
        inv.finalize_invoice(session, invoice.id, user_id=1)


def test_delete_location_cleans_cache_and_invoice_lines(session: Session) -> None:
    """A zeroed-out location deletes: cache rows go, invoice lines detach, and
    the movement ledger keeps its (now dangling) location_id as history."""
    drawer = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    session.add(ComponentLocation(component_id=1, location_id=drawer.id, quantity=0))
    session.add(
        InvoiceLine(
            invoice_id=1,
            component_id=1,
            quantity=2,
            unit_price=Decimal("1.00"),
            total_price=Decimal("2.00"),
            location_id=drawer.id,
        )
    )
    session.add(
        StockMovement(
            component_id=1,
            location_id=drawer.id,
            delta_quantity=0,
            reason=StockReason.CORRECTION,
            user_id=1,
        )
    )
    session.commit()

    ls.delete_location(session, drawer.id, user_id=1)

    assert ls.list_all(session) == []
    assert session.exec(select(ComponentLocation)).all() == []
    line = session.exec(select(InvoiceLine)).one()
    assert line.location_id is None
    movement = session.exec(select(StockMovement)).one()
    assert movement.location_id == drawer.id

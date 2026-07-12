"""Tests for stock_service: movements, cache consistency, invariants (D1)."""

from __future__ import annotations

import pytest
from app.models.enums import ContainerType, LocationType, StockReason
from app.seed import ensure_system_user
from app.services import component_service as cs
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import InsufficientStockError, NotFoundError, ValidationError
from sqlmodel import Session


@pytest.fixture
def fixture_ids(session: Session) -> tuple[int, int, int]:
    """Return (component_id, location_id, user_id) for stock tests."""
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    location = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    return component.id, location.id, user.id


def test_add_stock_updates_cache_and_ledger(fixture_ids, session: Session) -> None:
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=100,
        user_id=user_id,
    )
    assert ss.get_quantity(session, component_id, location_id) == 100
    assert ss.quantity_from_movements(session, component_id, location_id) == 100


def test_remove_stock_reduces_quantity(fixture_ids, session: Session) -> None:
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=100,
        user_id=user_id,
    )
    ss.remove_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=30,
        user_id=user_id,
    )
    assert ss.get_quantity(session, component_id, location_id) == 70
    assert ss.verify_cache_consistency(session)


def test_remove_more_than_available_raises(fixture_ids, session: Session) -> None:
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=10,
        user_id=user_id,
    )
    with pytest.raises(InsufficientStockError):
        ss.remove_stock(
            session,
            component_id=component_id,
            location_id=location_id,
            quantity=11,
            user_id=user_id,
        )
    # Failed removal must not have changed the cache.
    assert ss.get_quantity(session, component_id, location_id) == 10


def test_container_type_updates_existing_slot(fixture_ids, session: Session) -> None:
    """An explicit container type is applied even when the slot already exists (M6)."""
    component_id, location_id, user_id = fixture_ids

    def slot_container() -> ContainerType:
        cl = ss._find_component_location(session, component_id, location_id)
        assert cl is not None
        return cl.container_type

    # New slot with an explicit type.
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=10,
        user_id=user_id,
        container_type=ContainerType.REEL,
    )
    assert slot_container() is ContainerType.REEL

    # A later add with a new type updates the existing slot (was silently ignored).
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=5,
        user_id=user_id,
        container_type=ContainerType.BAG,
    )
    assert slot_container() is ContainerType.BAG
    assert ss.get_quantity(session, component_id, location_id) == 15

    # Omitting the type (default None) leaves the slot's type untouched.
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=1,
        user_id=user_id,
    )
    assert slot_container() is ContainerType.BAG


def test_new_slot_defaults_to_loose(fixture_ids, session: Session) -> None:
    """Without an explicit container type, a fresh slot is LOOSE (M6)."""
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=3,
        user_id=user_id,
    )
    cl = ss._find_component_location(session, component_id, location_id)
    assert cl is not None
    assert cl.container_type is ContainerType.LOOSE


def test_failed_removal_records_no_movement(fixture_ids, session: Session) -> None:
    """A rejected removal leaves neither a phantom movement nor a cache change."""
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=10,
        user_id=user_id,
    )
    with pytest.raises(InsufficientStockError):
        ss.remove_stock(
            session,
            component_id=component_id,
            location_id=location_id,
            quantity=11,
            user_id=user_id,
        )
    # Only the initial add survived; the guarded update rolled nothing forward.
    assert len(ss.list_movements(session, component_id)) == 1
    assert ss.get_quantity(session, component_id, location_id) == 10
    assert ss.verify_cache_consistency(session)


def test_removal_to_exactly_zero_succeeds(fixture_ids, session: Session) -> None:
    """The non-negative guard permits draining a slot to exactly zero."""
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=7,
        user_id=user_id,
    )
    ss.remove_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=7,
        user_id=user_id,
    )
    assert ss.get_quantity(session, component_id, location_id) == 0
    assert ss.verify_cache_consistency(session)


def test_removal_from_empty_slot_raises(fixture_ids, session: Session) -> None:
    """Removing from a slot that was never stocked is insufficient stock, not 500."""
    component_id, location_id, user_id = fixture_ids
    with pytest.raises(InsufficientStockError):
        ss.remove_stock(
            session,
            component_id=component_id,
            location_id=location_id,
            quantity=1,
            user_id=user_id,
        )


def test_duplicate_slot_is_rejected(fixture_ids, session: Session) -> None:
    """The (component, location) natural key forbids a second cache row (M2)."""
    from app.models.location import ComponentLocation
    from sqlalchemy.exc import IntegrityError

    component_id, location_id, _ = fixture_ids
    session.add(
        ComponentLocation(
            component_id=component_id, location_id=location_id, quantity=1
        )
    )
    session.add(
        ComponentLocation(
            component_id=component_id, location_id=location_id, quantity=2
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_non_positive_quantities_rejected(fixture_ids, session: Session) -> None:
    component_id, location_id, user_id = fixture_ids
    with pytest.raises(ValidationError):
        ss.add_stock(
            session,
            component_id=component_id,
            location_id=location_id,
            quantity=0,
            user_id=user_id,
        )
    with pytest.raises(ValidationError):
        ss.remove_stock(
            session,
            component_id=component_id,
            location_id=location_id,
            quantity=-5,
            user_id=user_id,
        )


def test_correction_can_be_negative_or_positive(fixture_ids, session: Session) -> None:
    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=50,
        user_id=user_id,
    )
    ss.apply_correction(
        session,
        component_id=component_id,
        location_id=location_id,
        delta=-5,
        user_id=user_id,
        note="damaged in handling",
    )
    assert ss.get_quantity(session, component_id, location_id) == 45
    movement = ss.apply_correction(
        session,
        component_id=component_id,
        location_id=location_id,
        delta=5,
        user_id=user_id,
    )
    assert movement.reason is StockReason.CORRECTION
    assert ss.get_quantity(session, component_id, location_id) == 50


def test_total_quantity_across_locations(fixture_ids, session: Session) -> None:
    component_id, location_id, user_id = fixture_ids
    other = ls.create_location(session, type=LocationType.DRAWER, name="D2")
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=40,
        user_id=user_id,
    )
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=other.id,
        quantity=60,
        user_id=user_id,
    )
    assert ss.total_quantity(session, component_id) == 100


def test_unknown_component_raises(session: Session) -> None:
    user = ensure_system_user(session)
    location = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    with pytest.raises(NotFoundError):
        ss.add_stock(
            session,
            component_id=999,
            location_id=location.id,
            quantity=1,
            user_id=user.id,
        )


def test_apply_correction_rejects_zero_delta(
    fixture_ids, session: Session
) -> None:
    component_id, location_id, user_id = fixture_ids
    with pytest.raises(ValidationError):
        ss.apply_correction(
            session,
            component_id=component_id,
            location_id=location_id,
            delta=0,
            user_id=user_id,
        )


def test_verify_cache_consistency_detects_drift(
    fixture_ids, session: Session
) -> None:
    """A cached quantity that no longer matches the movement ledger is caught."""
    from app.models.location import ComponentLocation
    from sqlmodel import select

    component_id, location_id, user_id = fixture_ids
    ss.add_stock(
        session,
        component_id=component_id,
        location_id=location_id,
        quantity=5,
        user_id=user_id,
    )
    assert ss.verify_cache_consistency(session)

    # Corrupt the cache behind the ledger's back.
    cl = session.exec(select(ComponentLocation)).one()
    cl.quantity = 999
    session.add(cl)
    session.commit()
    assert ss.verify_cache_consistency(session) is False

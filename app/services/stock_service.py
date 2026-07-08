"""Stock management business logic (spec §14-15, §17).

The stock-movement ledger is the source of truth for quantities;
``ComponentLocation.quantity`` is a cache updated in the *same transaction* as
the movement it results from (decision D1). Every mutation goes through
:func:`_record_movement`, which guarantees the two stay consistent and never
lets a location's quantity drop below zero.
"""

from __future__ import annotations

from sqlmodel import Session, func, select

from app.models.component import Component
from app.models.enums import ContainerType, StockReason
from app.models.location import ComponentLocation, Location
from app.models.stock import StockMovement
from app.services._common import require_entity
from app.services.errors import InsufficientStockError, ValidationError


def add_stock(
    session: Session,
    *,
    component_id: int,
    location_id: int,
    quantity: int,
    user_id: int,
    reason: StockReason = StockReason.PURCHASE,
    container_type: ContainerType = ContainerType.LOOSE,
    note: str | None = None,
    invoice_id: int | None = None,
) -> StockMovement:
    """Add ``quantity`` units of a component to a location (spec §14)."""
    if quantity <= 0:
        raise ValidationError("add_stock quantity must be positive")
    return _record_movement(
        session,
        component_id=component_id,
        location_id=location_id,
        delta=quantity,
        reason=reason,
        user_id=user_id,
        container_type=container_type,
        note=note,
        invoice_id=invoice_id,
    )


def remove_stock(
    session: Session,
    *,
    component_id: int,
    location_id: int,
    quantity: int,
    user_id: int,
    reason: StockReason = StockReason.USAGE,
    note: str | None = None,
) -> StockMovement:
    """Take ``quantity`` units of a component from a location (spec §15)."""
    if quantity <= 0:
        raise ValidationError("remove_stock quantity must be positive")
    return _record_movement(
        session,
        component_id=component_id,
        location_id=location_id,
        delta=-quantity,
        reason=reason,
        user_id=user_id,
        note=note,
    )


def apply_correction(
    session: Session,
    *,
    component_id: int,
    location_id: int,
    delta: int,
    user_id: int,
    note: str | None = None,
) -> StockMovement:
    """Apply a manual signed correction to a location's stock (spec §17)."""
    if delta == 0:
        raise ValidationError("correction delta must be non-zero")
    return _record_movement(
        session,
        component_id=component_id,
        location_id=location_id,
        delta=delta,
        reason=StockReason.CORRECTION,
        user_id=user_id,
        note=note,
    )


def get_quantity(session: Session, component_id: int, location_id: int) -> int:
    """Return the cached quantity of a component at a location (0 if none)."""
    cl = _find_component_location(session, component_id, location_id)
    return cl.quantity if cl is not None else 0


def total_quantity(session: Session, component_id: int) -> int:
    """Return the total cached quantity of a component across all locations."""
    total = session.exec(
        select(func.coalesce(func.sum(ComponentLocation.quantity), 0)).where(
            ComponentLocation.component_id == component_id
        )
    ).one()
    return int(total)


def quantity_from_movements(
    session: Session, component_id: int, location_id: int
) -> int:
    """Recompute quantity from the movement ledger (source of truth, D1)."""
    total = session.exec(
        select(func.coalesce(func.sum(StockMovement.delta_quantity), 0)).where(
            StockMovement.component_id == component_id,
            StockMovement.location_id == location_id,
        )
    ).one()
    return int(total)


def verify_cache_consistency(session: Session) -> bool:
    """Check that every cached quantity equals its movement-derived sum (D1)."""
    for cl in session.exec(select(ComponentLocation)).all():
        derived = quantity_from_movements(session, cl.component_id, cl.location_id)
        if derived != cl.quantity:
            return False
    return True


def _record_movement(
    session: Session,
    *,
    component_id: int,
    location_id: int,
    delta: int,
    reason: StockReason,
    user_id: int,
    container_type: ContainerType = ContainerType.LOOSE,
    note: str | None = None,
    invoice_id: int | None = None,
) -> StockMovement:
    """Write a movement and update the quantity cache atomically (decision D1)."""
    require_entity(session, Component, component_id, "component")
    require_entity(session, Location, location_id, "location")

    cl = _find_component_location(session, component_id, location_id)
    if cl is None:
        cl = ComponentLocation(
            component_id=component_id,
            location_id=location_id,
            quantity=0,
            container_type=container_type,
        )

    new_quantity = cl.quantity + delta
    if new_quantity < 0:
        raise InsufficientStockError(
            f"cannot remove {-delta}: only {cl.quantity} in stock at "
            f"location {location_id}"
        )
    cl.quantity = new_quantity

    movement = StockMovement(
        component_id=component_id,
        location_id=location_id,
        delta_quantity=delta,
        reason=reason,
        note=note,
        user_id=user_id,
        invoice_id=invoice_id,
    )

    session.add(cl)
    session.add(movement)
    session.commit()
    session.refresh(movement)
    return movement


def _find_component_location(
    session: Session, component_id: int, location_id: int
) -> ComponentLocation | None:
    return session.exec(
        select(ComponentLocation).where(
            ComponentLocation.component_id == component_id,
            ComponentLocation.location_id == location_id,
        )
    ).first()

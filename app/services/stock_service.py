"""Stock management business logic (spec §14-15, §17).

The stock-movement ledger is the source of truth for quantities;
``ComponentLocation.quantity`` is a cache updated in the *same transaction* as
the movement it results from (decision D1). Every mutation goes through
:func:`_record_movement`, which guarantees the two stay consistent and never
lets a location's quantity drop below zero.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.models.component import Component
from app.models.enums import ContainerType, StockReason
from app.models.location import ComponentLocation, Location
from app.models.stock import StockMovement
from app.services import audit_service
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
    container_type: ContainerType | None = None,
    note: str | None = None,
    invoice_id: int | None = None,
    commit: bool = True,
) -> StockMovement:
    """Add ``quantity`` units of a component to a location (spec §14).

    ``container_type`` sets the slot's packaging; ``None`` leaves an existing
    slot's type untouched and defaults new slots to ``LOOSE``. Pass
    ``commit=False`` to keep the movement in the caller's open transaction (used
    by invoice finalization so all lines commit atomically, D1).
    """
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
        commit=commit,
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


def total_quantities_by_component(
    session: Session,
) -> dict[int, int]:
    """Return a ``{component_id: total_quantity}`` map in a single query (§11)."""
    rows = session.exec(
        select(
            ComponentLocation.component_id,
            func.coalesce(func.sum(ComponentLocation.quantity), 0),
        ).group_by(col(ComponentLocation.component_id))
    ).all()
    return {component_id: int(total) for component_id, total in rows}


def list_component_locations(
    session: Session, component_id: int
) -> list[ComponentLocation]:
    """Return the locations where a component is currently stocked (qty > 0)."""
    return list(
        session.exec(
            select(ComponentLocation)
            .where(
                ComponentLocation.component_id == component_id,
                ComponentLocation.quantity > 0,
            )
            .order_by(col(ComponentLocation.location_id))
        ).all()
    )


def list_movements(session: Session, component_id: int) -> list[StockMovement]:
    """Return a component's stock movements, most recent first (§17)."""
    return list(
        session.exec(
            select(StockMovement)
            .where(StockMovement.component_id == component_id)
            .order_by(col(StockMovement.timestamp).desc(), col(StockMovement.id).desc())
        ).all()
    )


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
    container_type: ContainerType | None = None,
    note: str | None = None,
    invoice_id: int | None = None,
    commit: bool = True,
) -> StockMovement:
    """Write a movement and update the quantity cache atomically (decision D1).

    With ``commit=False`` the movement and cache update are only flushed, so
    they join the caller's transaction and commit (or roll back) together.
    """
    require_entity(session, Component, component_id, "component")
    require_entity(session, Location, location_id, "location")

    _apply_delta(session, component_id, location_id, delta, container_type)

    new_quantity = get_quantity(session, component_id, location_id)
    audit_service.record_change(
        session,
        entity_type="component",
        entity_id=component_id,
        field=audit_service.quantity_field(location_id),
        old_value=new_quantity - delta,
        new_value=new_quantity,
        user_id=user_id,
    )

    movement = StockMovement(
        component_id=component_id,
        location_id=location_id,
        delta_quantity=delta,
        reason=reason,
        note=note,
        user_id=user_id,
        invoice_id=invoice_id,
    )

    session.add(movement)
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(movement)
    return movement


def _apply_delta(
    session: Session,
    component_id: int,
    location_id: int,
    delta: int,
    container_type: ContainerType | None,
) -> None:
    """Move a slot's cached quantity by ``delta``, never below zero (D1).

    The change is a single guarded ``UPDATE`` whose ``WHERE`` clause also
    enforces the non-negative invariant, so concurrent movements on the same
    slot serialize without a read-modify-write race (lost updates or a negative
    quantity) on any backend with row-level locking. The first movement into a
    slot inserts the cache row; a lost insert race is retried as an update.

    A non-``None`` ``container_type`` is written to the slot (on both create and
    update); ``None`` leaves an existing slot's type untouched and defaults a
    new slot to ``LOOSE``.
    """
    values: dict[str, object] = {"quantity": col(ComponentLocation.quantity) + delta}
    if container_type is not None:
        values["container_type"] = container_type

    for _ in range(2):
        result = cast(
            "CursorResult[object]",
            session.execute(
                update(ComponentLocation)
                .where(
                    col(ComponentLocation.component_id) == component_id,
                    col(ComponentLocation.location_id) == location_id,
                    col(ComponentLocation.quantity) + delta >= 0,
                )
                .values(values)
            ),
        )
        if result.rowcount:
            return

        cl = _find_component_location(session, component_id, location_id)
        if cl is not None:
            # The slot exists but the guard rejected the update: it would go
            # negative.
            raise InsufficientStockError(
                f"cannot remove {-delta}: only {cl.quantity} in stock at "
                f"location {location_id}"
            )
        if delta < 0:
            raise InsufficientStockError(
                f"cannot remove {-delta}: nothing in stock at location {location_id}"
            )

        # First movement into this slot: create the cache row. If a concurrent
        # movement created it first, the unique key raises and we loop to take
        # the update path instead of writing a duplicate.
        try:
            with session.begin_nested():
                session.add(
                    ComponentLocation(
                        component_id=component_id,
                        location_id=location_id,
                        quantity=delta,
                        container_type=container_type or ContainerType.LOOSE,
                    )
                )
            return
        except IntegrityError:
            continue


def _find_component_location(
    session: Session, component_id: int, location_id: int
) -> ComponentLocation | None:
    return session.exec(
        select(ComponentLocation).where(
            ComponentLocation.component_id == component_id,
            ComponentLocation.location_id == location_id,
        )
    ).first()

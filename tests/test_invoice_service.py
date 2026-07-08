"""Tests for invoice_service: lines, totals, finalization, read-only lock."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from app.models.enums import LocationType
from app.seed import ensure_system_user
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import (
    InvoiceFinalizedError,
    NotFoundError,
    ValidationError,
)
from sqlmodel import Session


@pytest.fixture
def setup(session: Session) -> dict[str, int]:
    """Create a user, component and location for invoice tests."""
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    location = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    return {
        "user_id": user.id,
        "component_id": component.id,
        "location_id": location.id,
    }


def _new_invoice(session: Session) -> int:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    return invoice.id


def test_create_invoice_validates_required_fields(session: Session) -> None:
    with pytest.raises(ValidationError):
        inv.create_invoice(
            session,
            supplier="  ",
            invoice_number="INV-1",
            invoice_date=date(2026, 7, 8),
            currency="EUR",
        )


def test_add_line_computes_total_and_updates_net(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=100,
        unit_price=Decimal("0.05"),
    )
    assert line.total_price == Decimal("5.00")

    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.total_net == Decimal("5.00")


def test_add_line_rejects_non_positive_quantity(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    with pytest.raises(ValidationError):
        inv.add_line(
            session,
            invoice_id,
            component_id=setup["component_id"],
            quantity=0,
            unit_price=Decimal("0.05"),
        )


def test_net_recomputed_across_multiple_lines(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=4,
        unit_price=Decimal("2.50"),
    )
    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.total_net == Decimal("20.00")


def test_finalize_generates_stock_movements(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=100,
        unit_price=Decimal("0.05"),
        location_id=setup["location_id"],
    )
    finalized = inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])

    assert finalized.is_finalized is True
    assert finalized.total_gross == Decimal("5.00")
    # Stock arrived at the assigned location as a PURCHASE movement.
    assert ss.get_quantity(session, setup["component_id"], setup["location_id"]) == 100
    movements = ss.quantity_from_movements(
        session, setup["component_id"], setup["location_id"]
    )
    assert movements == 100
    assert ss.verify_cache_consistency(session)


def test_finalize_requires_at_least_one_line(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    with pytest.raises(ValidationError):
        inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])


def test_finalize_requires_location_on_every_line(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )  # no location assigned
    with pytest.raises(ValidationError):
        inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])


def test_finalized_invoice_is_read_only(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
        location_id=setup["location_id"],
    )
    inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])

    with pytest.raises(InvoiceFinalizedError):
        inv.add_line(
            session,
            invoice_id,
            component_id=setup["component_id"],
            quantity=1,
            unit_price=Decimal("1.00"),
            location_id=setup["location_id"],
        )
    with pytest.raises(InvoiceFinalizedError):
        inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])


def test_finalize_rejects_gross_less_than_net(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
        location_id=setup["location_id"],
    )
    with pytest.raises(ValidationError):
        inv.finalize_invoice(
            session,
            invoice_id,
            user_id=setup["user_id"],
            total_gross=Decimal("5.00"),
        )


def test_remove_line_updates_net(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.remove_line(session, line.id)
    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.total_net == Decimal("0")


def test_add_line_to_unknown_invoice_raises(setup, session: Session) -> None:
    with pytest.raises(NotFoundError):
        inv.add_line(
            session,
            999,
            component_id=setup["component_id"],
            quantity=1,
            unit_price=Decimal("1.00"),
        )

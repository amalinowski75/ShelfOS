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


def test_create_invoice_rejects_duplicate_number_per_supplier(
    session: Session,
) -> None:
    """The same supplier cannot reuse an invoice number; others can (M3)."""
    inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    with pytest.raises(ValidationError):
        inv.create_invoice(
            session,
            supplier="Mouser",
            invoice_number="INV-1",
            invoice_date=date(2026, 7, 9),
            currency="EUR",
        )
    # A different supplier may reuse the same number.
    inv.create_invoice(
        session,
        supplier="Digikey",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 9),
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


def test_net_total_is_exact_decimal_sum(setup, session: Session) -> None:
    """Net is summed as exact Decimal, not through a float SQL aggregate (L1)."""
    invoice_id = _new_invoice(session)
    for price in ("0.10", "0.20", "0.07"):
        inv.add_line(
            session,
            invoice_id,
            component_id=setup["component_id"],
            quantity=1,
            unit_price=Decimal(price),
        )
    # Summing the float result of SQL SUM would drift to 0.37000000000000005.
    total = inv._net_total(session, invoice_id)
    assert total == Decimal("0.37")
    assert isinstance(total, Decimal)


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


def test_finalize_is_atomic_when_a_movement_fails(
    setup, session: Session, monkeypatch
) -> None:
    """A failure partway through finalization rolls back everything (D1).

    Regression: movements used to commit per line before the finalized flag was
    set, so a mid-loop failure left committed stock behind and a re-finalize
    would double it. Now the flag flip and all movements share one transaction.
    """
    invoice_id = _new_invoice(session)
    for _ in range(2):
        inv.add_line(
            session,
            invoice_id,
            component_id=setup["component_id"],
            quantity=10,
            unit_price=Decimal("1.00"),
            location_id=setup["location_id"],
        )

    calls = {"n": 0}
    real_add_stock = ss.add_stock

    def flaky_add_stock(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the second line
            raise RuntimeError("boom")
        return real_add_stock(*args, **kwargs)

    monkeypatch.setattr(inv.stock_service, "add_stock", flaky_add_stock)

    with pytest.raises(RuntimeError):
        inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])
    session.rollback()

    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.is_finalized is False  # still a draft, can be retried
    # No stock leaked from the first (already-flushed) line.
    assert ss.get_quantity(session, setup["component_id"], setup["location_id"]) == 0
    assert (
        ss.quantity_from_movements(
            session, setup["component_id"], setup["location_id"]
        )
        == 0
    )


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
    inv.remove_line(session, invoice_id, line.id)
    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.total_net == Decimal("0")


def test_line_operations_reject_mismatched_invoice(setup, session: Session) -> None:
    """A line can only be mutated through the invoice it belongs to (M5)."""
    invoice_id = _new_invoice(session)
    other_id = inv.create_invoice(
        session,
        supplier="Digikey",
        invoice_number="INV-2",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    ).id
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )

    with pytest.raises(NotFoundError):
        inv.remove_line(session, other_id, line.id)
    with pytest.raises(NotFoundError):
        inv.set_line_location(session, other_id, line.id, setup["location_id"])

    # The line is untouched and still operable through its real invoice.
    assert session.get(inv.InvoiceLine, line.id) is not None
    inv.set_line_location(session, invoice_id, line.id, setup["location_id"])


def test_add_line_to_unknown_invoice_raises(setup, session: Session) -> None:
    with pytest.raises(NotFoundError):
        inv.add_line(
            session,
            999,
            component_id=setup["component_id"],
            quantity=1,
            unit_price=Decimal("1.00"),
        )


def test_list_invoices_orders_newest_first_and_filters(
    setup, session: Session
) -> None:
    older = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-OLD",
        invoice_date=date(2026, 7, 1),
        currency="EUR",
    )
    newer = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-NEW",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    inv.add_line(
        session,
        newer.id,
        component_id=setup["component_id"],
        quantity=5,
        unit_price=Decimal("1.00"),
        location_id=setup["location_id"],
    )
    inv.finalize_invoice(session, newer.id, user_id=setup["user_id"])

    # Newest invoice_date first.
    assert [i.id for i in inv.list_invoices(session)] == [newer.id, older.id]
    # Filtered by finalization state.
    assert [i.id for i in inv.list_invoices(session, finalized=True)] == [newer.id]
    assert [i.id for i in inv.list_invoices(session, finalized=False)] == [older.id]


def test_get_invoice_detail_pairs_each_line(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=3,
        unit_price=Decimal("2.00"),
    )
    invoice, pairs = inv.get_invoice_detail(session, invoice_id)
    assert invoice.id == invoice_id
    assert len(pairs) == 1
    line, component = pairs[0]
    assert line.component_id == setup["component_id"]
    assert component is not None
    assert component.id == setup["component_id"]

    # Existence is checked (once).
    with pytest.raises(NotFoundError):
        inv.get_invoice_detail(session, 9999)


def test_get_invoice_detail_tolerates_deleted_component(
    setup, session: Session
) -> None:
    """A line whose component was hard-deleted still reads, paired with None."""
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=1,
        unit_price=Decimal("1.00"),
    )
    # §20 hard delete removes the component but keeps the line as history.
    cs.hard_delete_component(session, setup["component_id"])

    _invoice, pairs = inv.get_invoice_detail(session, invoice_id)
    assert len(pairs) == 1
    line, component = pairs[0]
    assert line.component_id == setup["component_id"]
    assert component is None


def test_update_invoice_edits_metadata_and_audits(setup, session: Session) -> None:
    from app.services import audit_service as audit

    invoice_id = _new_invoice(session)  # Mouser / INV-1
    inv.update_invoice(
        session, invoice_id, supplier="Digikey", notes="rush", user_id=setup["user_id"]
    )
    updated = session.get(inv.Invoice, invoice_id)
    assert updated is not None
    assert updated.supplier == "Digikey"
    assert updated.notes == "rush"
    assert updated.invoice_number == "INV-1"  # untouched

    fields = {
        e.field
        for e in audit.list_entries(
            session, entity_type="invoice", entity_id=invoice_id
        )
    }
    assert {"supplier", "notes"} <= fields
    assert "invoice_number" not in fields  # unchanged -> not audited


def test_update_invoice_rejects_when_finalized(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=1,
        unit_price=Decimal("1.00"),
        location_id=setup["location_id"],
    )
    inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])
    with pytest.raises(InvoiceFinalizedError):
        inv.update_invoice(session, invoice_id, supplier="Digikey")


def test_update_invoice_rejects_duplicate_number(setup, session: Session) -> None:
    _new_invoice(session)  # Mouser / INV-1
    other = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-2",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    with pytest.raises(ValidationError):
        inv.update_invoice(session, other.id, invoice_number="INV-1")


def test_update_invoice_rejects_empty_required(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    with pytest.raises(ValidationError):
        inv.update_invoice(session, invoice_id, supplier="   ")


def test_update_line_recomputes_total_and_net_and_audits(
    setup, session: Session
) -> None:
    from app.services import audit_service as audit

    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.update_line(
        session,
        invoice_id,
        line.id,
        quantity=5,
        unit_price=Decimal("3.00"),
        user_id=setup["user_id"],
    )
    updated = session.get(inv.InvoiceLine, line.id)
    assert updated is not None
    assert updated.quantity == 5
    assert updated.unit_price == Decimal("3.00")
    assert updated.total_price == Decimal("15.00")  # 10*1.00 -> 5*3.00

    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.total_net == Decimal("15.00")

    fields = {
        e.field
        for e in audit.list_entries(
            session, entity_type="invoice_line", entity_id=line.id
        )
    }
    assert {"quantity", "unit_price", "total_price"} <= fields


def test_update_line_validates_quantity_and_price(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    with pytest.raises(ValidationError):
        inv.update_line(session, invoice_id, line.id, quantity=0)
    with pytest.raises(ValidationError):
        inv.update_line(session, invoice_id, line.id, unit_price=Decimal("-1"))


def test_update_line_rejects_when_finalized(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=1,
        unit_price=Decimal("1.00"),
        location_id=setup["location_id"],
    )
    inv.finalize_invoice(session, invoice_id, user_id=setup["user_id"])
    with pytest.raises(InvoiceFinalizedError):
        inv.update_line(session, invoice_id, line.id, quantity=2)


def test_update_line_rejects_mismatched_invoice(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    other = inv.create_invoice(
        session,
        supplier="Digikey",
        invoice_number="INV-2",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    with pytest.raises(NotFoundError):
        inv.update_line(session, other.id, line.id, quantity=2)


def test_update_invoice_no_op_writes_no_audit(setup, session: Session) -> None:
    from app.services import audit_service as audit

    invoice_id = _new_invoice(session)  # Mouser / INV-1 / EUR
    # Re-sending the current values changes nothing, so nothing is audited.
    inv.update_invoice(
        session, invoice_id, supplier="Mouser", currency="EUR", user_id=setup["user_id"]
    )
    assert (
        audit.list_entries(session, entity_type="invoice", entity_id=invoice_id) == []
    )


def test_update_line_no_op_writes_no_audit(setup, session: Session) -> None:
    from app.services import audit_service as audit

    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=4,
        unit_price=Decimal("1.00"),
    )
    inv.update_line(
        session,
        invoice_id,
        line.id,
        quantity=4,
        unit_price=Decimal("1.00"),
        user_id=setup["user_id"],
    )
    assert (
        audit.list_entries(session, entity_type="invoice_line", entity_id=line.id) == []
    )


def test_update_line_accepts_zero_unit_price(setup, session: Session) -> None:
    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=4,
        unit_price=Decimal("1.00"),
    )
    updated = inv.update_line(session, invoice_id, line.id, unit_price=Decimal("0"))
    assert updated.unit_price == Decimal("0")
    assert updated.total_price == Decimal("0")
    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.total_net == Decimal("0")


def test_update_invoice_empty_string_clears_notes(setup, session: Session) -> None:
    """An empty string blanks notes; only ``None`` means "leave unchanged".

    The web edit form relies on this to distinguish a cleared field from an
    untouched one.
    """
    invoice_id = _new_invoice(session)
    inv.update_invoice(session, invoice_id, notes="rush")
    inv.update_invoice(session, invoice_id, notes="")
    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None and invoice.notes == ""

    # None leaves the (now empty) value untouched.
    inv.update_invoice(session, invoice_id, supplier="Digikey")
    invoice = session.get(inv.Invoice, invoice_id)
    assert invoice is not None and invoice.notes == ""


def test_update_line_empty_string_clears_part_number(
    setup, session: Session
) -> None:
    """Same contract for a line's supplier part number (cleared vs unchanged)."""
    invoice_id = _new_invoice(session)
    line = inv.add_line(
        session,
        invoice_id,
        component_id=setup["component_id"],
        quantity=1,
        unit_price=Decimal("1.00"),
        supplier_part_number="SPN-9",
    )
    inv.update_line(session, invoice_id, line.id, supplier_part_number="")
    cleared = session.get(inv.InvoiceLine, line.id)
    assert cleared is not None and cleared.supplier_part_number == ""

    # None leaves it as-is.
    inv.update_line(session, invoice_id, line.id, quantity=2)
    kept = session.get(inv.InvoiceLine, line.id)
    assert kept is not None and kept.supplier_part_number == ""

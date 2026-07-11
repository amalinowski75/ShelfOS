"""Invoice business logic (spec §9, §16).

An invoice is built incrementally: create it, add lines linked to components,
assign a destination location to each line, then finalize. Finalization locks
the invoice read-only and generates purchase stock movements (decision D1) for
every line via :mod:`app.services.stock_service`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col, func, select

from app.models.component import Component
from app.models.enums import StockReason
from app.models.invoice import Invoice, InvoiceLine
from app.models.location import Location
from app.services import stock_service
from app.services._common import require_entity
from app.services.errors import (
    InvoiceFinalizedError,
    NotFoundError,
    ValidationError,
)


def create_invoice(
    session: Session,
    *,
    supplier: str,
    invoice_number: str,
    invoice_date: date,
    currency: str,
    notes: str | None = None,
    file_path: str | None = None,
) -> Invoice:
    """Create a new (non-finalized) invoice with zero totals."""
    if not supplier.strip():
        raise ValidationError("invoice supplier must not be empty")
    if not invoice_number.strip():
        raise ValidationError("invoice number must not be empty")
    if not currency.strip():
        raise ValidationError("invoice currency must not be empty")
    existing = session.exec(
        select(Invoice.id).where(
            Invoice.supplier == supplier,
            Invoice.invoice_number == invoice_number,
        )
    ).first()
    if existing is not None:
        raise ValidationError(
            f"invoice {invoice_number!r} already exists for supplier {supplier!r}"
        )

    invoice = Invoice(
        supplier=supplier,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        currency=currency,
        notes=notes,
        file_path=file_path,
    )
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice


def add_line(
    session: Session,
    invoice_id: int,
    *,
    component_id: int,
    quantity: int,
    unit_price: Decimal,
    supplier_part_number: str | None = None,
    location_id: int | None = None,
) -> InvoiceLine:
    """Add a line to a draft invoice; ``total_price`` is computed automatically."""
    invoice = _require_draft(session, invoice_id)
    require_entity(session, Component, component_id, "component")
    if quantity <= 0:
        raise ValidationError("invoice line quantity must be positive")
    if unit_price < 0:
        raise ValidationError("invoice line unit price must not be negative")
    if location_id is not None:
        require_entity(session, Location, location_id, "location")

    line = InvoiceLine(
        invoice_id=invoice_id,
        component_id=component_id,
        supplier_part_number=supplier_part_number,
        quantity=quantity,
        unit_price=unit_price,
        total_price=unit_price * quantity,
        location_id=location_id,
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    _recompute_net(session, invoice)
    session.refresh(line)
    return line


def set_line_location(
    session: Session, invoice_id: int, line_id: int, location_id: int
) -> InvoiceLine:
    """Assign a destination stock location to a line (spec §16 step 6)."""
    line = _require_line_of_invoice(session, invoice_id, line_id)
    _require_draft(session, line.invoice_id)
    require_entity(session, Location, location_id, "location")
    line.location_id = location_id
    session.add(line)
    session.commit()
    session.refresh(line)
    return line


def remove_line(session: Session, invoice_id: int, line_id: int) -> None:
    """Remove a line from a draft invoice and refresh totals."""
    line = _require_line_of_invoice(session, invoice_id, line_id)
    invoice = _require_draft(session, line.invoice_id)
    session.delete(line)
    session.commit()
    _recompute_net(session, invoice)


def get_lines(session: Session, invoice_id: int) -> list[InvoiceLine]:
    """Return all lines of an invoice ordered by id."""
    require_entity(session, Invoice, invoice_id, "invoice")
    return list(
        session.exec(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice_id)
            .order_by(InvoiceLine.id)  # type: ignore[arg-type]
        ).all()
    )


def finalize_invoice(
    session: Session,
    invoice_id: int,
    *,
    user_id: int,
    total_gross: Decimal | None = None,
) -> Invoice:
    """Finalize an invoice: lock it and generate purchase stock movements.

    Requires at least one line (spec §9) and a destination location on every
    line so stock movements can be generated (decision D1). After finalization
    the invoice is read-only.
    """
    invoice = _require_draft(session, invoice_id)
    lines = get_lines(session, invoice_id)
    if not lines:
        raise ValidationError("cannot finalize an invoice with no lines")
    if any(line.location_id is None for line in lines):
        raise ValidationError(
            "every invoice line must have a location before finalization"
        )

    net = _net_total(session, invoice_id)
    if total_gross is not None:
        if total_gross < net:
            raise ValidationError("total_gross must not be less than total_net")
        gross = total_gross
    else:
        gross = net

    # Claim finalization atomically: flip the flag only if the invoice is still a
    # draft, in the same transaction as the stock movements below. A concurrent
    # finalize (or a retry after a mid-loop failure) sees rowcount 0 and aborts,
    # so movements are never generated twice (decision D1).
    claimed = cast(
        "CursorResult[object]",
        session.execute(
            update(Invoice)
            .where(col(Invoice.id) == invoice_id, col(Invoice.is_finalized).is_(False))
            .values(is_finalized=True, total_net=net, total_gross=gross)
        ),
    )
    if claimed.rowcount != 1:
        raise InvoiceFinalizedError(f"invoice {invoice_id} is finalized (read-only)")

    # Generate a purchase movement per line without committing; the single
    # commit below makes the flag flip and every movement succeed or fail as one.
    for line in lines:
        assert line.location_id is not None  # guarded above
        stock_service.add_stock(
            session,
            component_id=line.component_id,
            location_id=line.location_id,
            quantity=line.quantity,
            user_id=user_id,
            reason=StockReason.PURCHASE,
            invoice_id=invoice_id,
            commit=False,
        )

    session.commit()
    session.refresh(invoice)
    return invoice


def list_purchase_history(
    session: Session, component_id: int
) -> list[tuple[InvoiceLine, Invoice]]:
    """Return finalized invoice lines for a component, newest first (spec §12)."""
    rows = session.exec(
        select(InvoiceLine, Invoice)
        .join(Invoice, col(InvoiceLine.invoice_id) == Invoice.id)
        .where(
            InvoiceLine.component_id == component_id,
            col(Invoice.is_finalized).is_(True),
        )
        .order_by(col(Invoice.invoice_date).desc(), col(Invoice.id).desc())
    ).all()
    return [(line, invoice) for line, invoice in rows]


def _require_line_of_invoice(
    session: Session, invoice_id: int, line_id: int
) -> InvoiceLine:
    """Fetch a line and ensure it actually belongs to ``invoice_id``.

    The line-mutation routes carry both an ``{invoice_id}`` and a ``{line_id}``
    in the URL; without this check the invoice id is ignored and a line can be
    mutated through any invoice's path (a wrong-invoice / IDOR-style defect).
    """
    line = require_entity(session, InvoiceLine, line_id, "invoice line")
    if line.invoice_id != invoice_id:
        raise NotFoundError(
            f"invoice line {line_id} does not belong to invoice {invoice_id}"
        )
    return line


def _require_draft(session: Session, invoice_id: int) -> Invoice:
    """Fetch an invoice and ensure it is not finalized (read-only, §16)."""
    invoice = require_entity(session, Invoice, invoice_id, "invoice")
    if invoice.is_finalized:
        raise InvoiceFinalizedError(f"invoice {invoice_id} is finalized (read-only)")
    return invoice


def _net_total(session: Session, invoice_id: int | None) -> Decimal:
    """Return the sum of an invoice's line totals (no persistence)."""
    total = session.exec(
        select(func.coalesce(func.sum(InvoiceLine.total_price), 0)).where(
            InvoiceLine.invoice_id == invoice_id
        )
    ).one()
    return Decimal(total)


def _recompute_net(session: Session, invoice: Invoice) -> Decimal:
    """Recalculate ``total_net`` as the sum of line totals and persist it."""
    invoice.total_net = _net_total(session, invoice.id)
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice.total_net

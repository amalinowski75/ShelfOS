"""Invoice endpoints (spec §9, §16)."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    InvoiceCreate,
    InvoiceDetailRead,
    InvoiceFinalize,
    InvoiceLineComponentRead,
    InvoiceLineCreate,
    InvoiceLineRead,
    LineLocationSet,
)
from app.auth.deps import current_user_id
from app.models.invoice import Invoice, InvoiceLine
from app.services import invoice_service as inv

router = APIRouter(prefix="/api/invoices", tags=["invoices"])


@router.post("", response_model=Invoice, status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: InvoiceCreate, session: Session = Depends(get_session)
) -> Invoice:
    return inv.create_invoice(
        session,
        supplier=payload.supplier,
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        currency=payload.currency,
        notes=payload.notes,
        file_path=payload.file_path,
    )


@router.get("", response_model=list[Invoice])
def list_invoices(
    finalized: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[Invoice]:
    """List invoices newest first, optionally filtered by finalization state."""
    return inv.list_invoices(
        session, finalized=finalized, limit=limit, offset=offset
    )


@router.get("/{invoice_id}", response_model=InvoiceDetailRead)
def get_invoice(
    invoice_id: int, session: Session = Depends(get_session)
) -> InvoiceDetailRead:
    """Return an invoice header, its totals and its lines with component identity."""
    invoice, lines = inv.get_invoice_detail(session, invoice_id)
    return InvoiceDetailRead(
        id=cast(int, invoice.id),
        supplier=invoice.supplier,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        currency=invoice.currency,
        total_net=invoice.total_net,
        total_gross=invoice.total_gross,
        file_path=invoice.file_path,
        notes=invoice.notes,
        is_finalized=invoice.is_finalized,
        lines=[
            InvoiceLineRead(
                id=cast(int, line.id),
                invoice_id=line.invoice_id,
                component_id=line.component_id,
                supplier_part_number=line.supplier_part_number,
                quantity=line.quantity,
                unit_price=line.unit_price,
                total_price=line.total_price,
                location_id=line.location_id,
                component=(
                    InvoiceLineComponentRead(
                        id=cast(int, component.id),
                        manufacturer=component.manufacturer,
                        mpn=component.mpn,
                        type_id=component.type_id,
                    )
                    if component is not None
                    else None
                ),
            )
            for line, component in lines
        ],
    )


@router.get("/{invoice_id}/lines", response_model=list[InvoiceLine])
def list_lines(
    invoice_id: int, session: Session = Depends(get_session)
) -> list[InvoiceLine]:
    return inv.get_lines(session, invoice_id)


@router.post(
    "/{invoice_id}/lines",
    response_model=InvoiceLine,
    status_code=status.HTTP_201_CREATED,
)
def add_line(
    invoice_id: int,
    payload: InvoiceLineCreate,
    session: Session = Depends(get_session),
) -> InvoiceLine:
    return inv.add_line(
        session,
        invoice_id,
        component_id=payload.component_id,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        supplier_part_number=payload.supplier_part_number,
        location_id=payload.location_id,
    )


@router.put("/{invoice_id}/lines/{line_id}/location", response_model=InvoiceLine)
def set_line_location(
    invoice_id: int,
    line_id: int,
    payload: LineLocationSet,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> InvoiceLine:
    return inv.set_line_location(
        session, invoice_id, line_id, payload.location_id, user_id=user_id
    )


@router.delete("/{invoice_id}/lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_line(
    invoice_id: int,
    line_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    inv.remove_line(session, invoice_id, line_id, user_id=user_id)


@router.post("/{invoice_id}/finalize", response_model=Invoice)
def finalize_invoice(
    invoice_id: int,
    payload: InvoiceFinalize,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> Invoice:
    return inv.finalize_invoice(
        session, invoice_id, user_id=user_id, total_gross=payload.total_gross
    )

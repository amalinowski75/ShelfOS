"""Invoice and invoice line models (spec §9, §16).

Monetary amounts use ``Decimal`` and a single currency per invoice (decision D5).
An invoice becomes read-only once finalized.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# Monetary precision shared by all amount columns (decision D5).
_MONEY_DIGITS = 18
_MONEY_PLACES = 6


class Invoice(SQLModel, table=True):
    __tablename__ = "invoices"
    # A supplier does not issue two invoices with the same number; different
    # suppliers may reuse a number, so uniqueness is per (supplier, number).
    __table_args__ = (
        UniqueConstraint(
            "supplier", "invoice_number", name="uq_invoice_supplier_number"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    supplier: str
    invoice_number: str
    invoice_date: date
    currency: str
    total_net: Decimal = Field(
        default=Decimal(0), max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES
    )
    total_gross: Decimal = Field(
        default=Decimal(0), max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES
    )
    file_path: str | None = Field(default=None)
    notes: str | None = Field(default=None)
    is_finalized: bool = Field(default=False)


class InvoiceLine(SQLModel, table=True):
    __tablename__ = "invoice_lines"

    id: int | None = Field(default=None, primary_key=True)
    invoice_id: int = Field(foreign_key="invoices.id")
    component_id: int = Field(foreign_key="components.id")
    supplier_part_number: str | None = Field(default=None)
    quantity: int
    unit_price: Decimal = Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
    total_price: Decimal = Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
    # Destination stock location, assigned before finalization (spec §16 step 6).
    location_id: int | None = Field(default=None, foreign_key="locations.id")

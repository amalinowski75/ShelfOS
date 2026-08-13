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


class InvoiceImportLine(SQLModel, table=True):
    """A parsed PDF-import line staged for review; its component is created at finalize.

    Import never creates a component immediately — it carries what it read off the
    invoice here (enriched where possible) and lets the user review each line on the
    draft before committing. A row is **ready** once it has a ``type_id`` and a
    ``location_id``; a row with no ``type_id`` still **needs review**. Finalizing the
    invoice materialises every ready row into a real component + line (deleting the
    staging row), so nothing pollutes the catalog unless the invoice is
    actually committed — and a wrong auto-classification can be corrected here first.
    """

    __tablename__ = "invoice_import_lines"

    id: int | None = Field(default=None, primary_key=True)
    invoice_id: int = Field(foreign_key="invoices.id")
    line_no: int
    supplier_part_number: str | None = Field(default=None)
    mpn: str | None = Field(default=None)
    manufacturer: str | None = Field(default=None)
    description: str | None = Field(default=None)
    package: str | None = Field(default=None)
    quantity: int
    unit_price: Decimal = Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
    shop_key: str
    # The resolved component type (auto-inferred or user-picked). None → still needs a
    # type before it can be created; set → "ready" to materialise at finalize.
    type_id: int | None = Field(default=None, foreign_key="component_types.id")
    # Destination stock location, assigned during review; required before finalize.
    location_id: int | None = Field(default=None, foreign_key="locations.id")
    # Why it still needs the user (e.g. "no matching type"); "" once ready.
    reason: str

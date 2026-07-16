"""BOM (bill of materials) models (spec §21/§22).

A ``Bom`` is an imported KiCad BOM; each ``BomLine`` holds one parsed line (a
group of reference designators sharing a value/footprint/MPN). Lines store only
the parsed input — matching against inventory and stock is computed live at
report time, so the report always reflects current stock. The original CSV is
kept as an attachment (``entity_type="bom"``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Bom(SQLModel, table=True):
    __tablename__ = "boms"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    source_filename: str | None = Field(default=None)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BomLine(SQLModel, table=True):
    __tablename__ = "bom_lines"

    id: int | None = Field(default=None, primary_key=True)
    bom_id: int = Field(foreign_key="boms.id", index=True)
    references: str  # raw designator list, e.g. "R1, R2, R3"
    reference_prefix: str | None = Field(default=None)  # "R", "C", …
    category: str | None = Field(default=None)  # inferred: resistor/capacitor/…
    value: str | None = Field(default=None)  # KiCad value, e.g. "10k 1%"
    footprint: str | None = Field(default=None)
    mpn: str | None = Field(default=None)
    manufacturer: str | None = Field(default=None)
    quantity: int = Field(default=1)  # parts on the board for this line

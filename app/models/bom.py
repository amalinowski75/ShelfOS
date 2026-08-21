"""BOM (bill of materials) models (spec §21/§22).

A ``Bom`` is an imported KiCad BOM; each ``BomLine`` holds one parsed line (a
group of reference designators sharing a value/footprint/MPN). Lines store only
the parsed input — matching against inventory and stock is computed live at
report time, so the report always reflects current stock. The original CSV is
kept as an attachment (``entity_type="bom"``).

A ``BomLineAssignment`` is the one exception to "lines store only the parsed
input": the human decision that a given line is built from a particular
component, whatever its MPN says (or fails to say).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
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


class BomLineAssignment(SQLModel, table=True):
    """"This line is built from THIS component" — a stored human decision.

    Report matching is by MPN, which has nothing to say for a line whose MPN is
    blank, names a part no one stocks, or names the one that ran out. An
    assignment overrides that lookup: the line's stock and status are read from
    the chosen component instead.

    Keyed by the line's ``references`` (its designator group, e.g. "R1,R2,R3")
    rather than by ``bom_line_id``: re-importing a BOM deletes and recreates every
    ``BomLine``, so a foreign key to the row would not survive a "Reload from
    CSV". The designator group is how a human names the line, and it is stable
    across a re-parse of the same file.
    """

    __tablename__ = "bom_line_assignments"
    __table_args__ = (UniqueConstraint("bom_id", "references"),)

    id: int | None = Field(default=None, primary_key=True)
    bom_id: int = Field(foreign_key="boms.id", index=True)
    references: str
    component_id: int = Field(foreign_key="components.id")
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BomLineOrdered(SQLModel, table=True):
    """"The parts for this line have been ordered" — a stored human decision.

    A row IS the flag: present means ordered, absent means not. That keeps the
    truth in one place (no row that says ``False``) and records who ticked it and
    when for free.

    A separate table rather than a column on ``BomLine`` for two reasons. The
    schema has no migrations — ``create_all`` adds a missing TABLE to an existing
    database but never a missing COLUMN — so a new table reaches production on a
    restart while a new column would not. And lines are deleted and recreated by a
    re-import, so anything kept on them would not survive "Reload from CSV";
    keying by ``references`` (the designator group) does, exactly as
    :class:`BomLineAssignment` does.
    """

    __tablename__ = "bom_line_ordered"
    __table_args__ = (UniqueConstraint("bom_id", "references"),)

    id: int | None = Field(default=None, primary_key=True)
    bom_id: int = Field(foreign_key="boms.id", index=True)
    references: str
    marked_by: int = Field(foreign_key="users.id")
    marked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

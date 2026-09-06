"""A BOM taken off the shelves, recorded so it can be audited and undone.

Pressing "Take parts" removes every line's component from stock in one
transaction and leaves this snapshot behind: a header (:class:`BomTake`), what
each line was meant to give and what it actually gave (:class:`BomTakeLine`), and
one row per stock movement saying which location it came from
(:class:`BomTakeAllocation`).

Three tables rather than a column on ``stock_movements``. The schema has no
migrations — ``create_all`` adds a missing TABLE to an existing database but never
a missing COLUMN — so a ``bom_take_id`` beside the existing ``invoice_id`` would
reach production only by recreating the database and destroying the stock it
records. The link therefore points the other way: an allocation names its
movement, and ``stock_movements`` is untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class BomTake(SQLModel, table=True):
    """One run of "take this BOM off the shelves"."""

    __tablename__ = "bom_takes"

    id: int | None = Field(default=None, primary_key=True)
    bom_id: int = Field(foreign_key="boms.id", index=True)
    # Stored, not derived from the BOM's name and `created_at`: this string is
    # copied verbatim into every movement's note, and renaming the BOM afterwards
    # must not make those notes say something that was never written.
    name: str
    boards: int = Field(default=1)
    # The gathering parent the take drew from first. Nullable so a snapshot whose
    # temporary tree has since been deleted reads as "—" rather than breaking the
    # page (SQLite does not enforce this key, exactly as the ledger relies on).
    source_location_id: int | None = Field(default=None, foreign_key="locations.id")
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Undo marks the snapshot, never deletes it: what happened still happened.
    # `reversed_at IS NULL` is also the status and the concurrency guard, so a
    # double-clicked Undo cannot return the same stock twice.
    reversed_at: datetime | None = Field(default=None)
    reversed_by: int | None = Field(default=None, foreign_key="users.id")
    reversal_reason: str | None = Field(default=None)


class BomTakeLine(SQLModel, table=True):
    """What one BOM line was asked for, and what the shelves actually gave.

    Keyed by ``references`` rather than ``bom_lines.id`` for the reason
    :class:`~app.models.bom.BomLineAssignment` gives: a re-import deletes and
    recreates every line, and a snapshot must still read correctly afterwards.

    The shortfall is ``requested_quantity - taken_quantity`` and is deliberately
    not stored — a third number can disagree with the two it comes from.
    """

    __tablename__ = "bom_take_lines"

    id: int | None = Field(default=None, primary_key=True)
    take_id: int = Field(foreign_key="bom_takes.id", index=True)
    references: str
    # What was taken, frozen: re-assigning the BOM line later must not rewrite
    # what this run actually pulled off the shelf.
    component_id: int = Field(foreign_key="components.id")
    requested_quantity: int
    taken_quantity: int


class BomTakeAllocation(SQLModel, table=True):
    """One stock movement made by a take, and the location it came from.

    ``movement_id`` is the movement→snapshot link, in both directions: the take's
    own movement and, once undone, the one that put the parts back.
    """

    __tablename__ = "bom_take_allocations"

    id: int | None = Field(default=None, primary_key=True)
    take_id: int = Field(foreign_key="bom_takes.id", index=True)
    take_line_id: int = Field(foreign_key="bom_take_lines.id", index=True)
    location_id: int = Field(foreign_key="locations.id")
    quantity: int
    # Unique: a movement belongs to at most one take.
    movement_id: int = Field(foreign_key="stock_movements.id", index=True, unique=True)
    reversal_movement_id: int | None = Field(
        default=None, foreign_key="stock_movements.id", index=True
    )

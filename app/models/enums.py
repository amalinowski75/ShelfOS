"""Controlled enumerations used across the domain model (decision D7).

Every enum is a ``str`` enum whose member value is the canonical token stored in
the database. Columns are created with :func:`enum_column` so SQLAlchemy persists
the member *value* (not its name), keeping stored tokens stable and readable.
"""

from __future__ import annotations

import enum

from sqlalchemy import Column
from sqlalchemy import Enum as SAEnum


class MountingType(enum.StrEnum):
    """How a component is physically mounted (spec §4)."""

    SMT = "SMT"
    THT = "THT"
    PANEL = "Panel"
    WIRE = "Wire"
    OTHER = "Other"


class ContainerType(enum.StrEnum):
    """Physical container a stock quantity is stored in (spec §8)."""

    REEL = "reel"
    BAG = "bag"
    FEEDER = "feeder"
    LOOSE = "loose"
    BOX = "box"


class LocationType(enum.StrEnum):
    """Level in the storage-location hierarchy (spec §7)."""

    ROOM = "room"
    RACK = "rack"
    SHELF = "shelf"
    PARTITION = "partition"
    DRAWER = "drawer"
    COMPARTMENT = "compartment"
    # Future types (spec §7):
    FEEDER = "feeder"
    BOX = "box"


class StockReason(enum.StrEnum):
    """Reason a stock movement was recorded (spec §17)."""

    PURCHASE = "purchase"
    CORRECTION = "correction"
    USAGE = "usage"
    DAMAGED_LOST = "damaged_lost"


class ComponentStatus(enum.StrEnum):
    """Lifecycle status of a component (spec §20)."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    OBSOLETE = "obsolete"
    HIDDEN = "hidden"


class ParameterDataType(enum.StrEnum):
    """Data type of a parameter definition (decision D6)."""

    NUMBER = "number"
    TEXT = "text"
    BOOL = "bool"
    ENUM = "enum"


class AttachmentKind(enum.StrEnum):
    """Kind of attached file (spec §10)."""

    PHOTO = "photo"
    DATASHEET = "datasheet"
    INVOICE_PDF = "invoice_pdf"
    NOTE = "note"
    OTHER = "other"


class UserRole(enum.StrEnum):
    """Access role of a user (spec §18)."""

    ADMIN = "admin"
    USER = "user"
    READ_ONLY = "read-only"


def enum_column(enum_cls: type[enum.Enum], **kwargs: object) -> Column:  # type: ignore[type-arg]
    """Build a SQLAlchemy column that stores an enum by its *value*.

    Using ``values_callable`` ensures the human-readable token (e.g.
    ``"read-only"``) is persisted rather than the Python member name.
    """
    return Column(
        SAEnum(enum_cls, values_callable=lambda cls: [m.value for m in cls]),
        **kwargs,  # type: ignore[arg-type]
    )

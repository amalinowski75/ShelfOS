"""Generic audit log (spec §19, decision D9).

A single table records field-level changes across entities (components,
invoices, parameters, locations).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class AuditLog(SQLModel, table=True):
    """One field-level change.

    This is the only table in ShelfOS that grows without bound and is never
    pruned, so its reads are indexed rather than left to a scan that is cheap
    today and the page's problem in two years. The reader walks it newest-first
    by ``(timestamp, id)`` -- which is also the key it pages by -- and narrows it
    by who and by kind, so those are the three indexes.

    ``timestamp`` is UTC (``datetime.now(UTC)``); SQLite stores it without the
    offset, so anything displaying it has to say so rather than let a reader
    assume local time.
    """

    __tablename__ = "audit_log"
    # Composite, in the order the log is read: the newest page is then a range
    # scan of ``limit`` rows instead of sorting the whole log to throw away all
    # but the first screenful.
    __table_args__ = (Index("ix_audit_log_timestamp_id", "timestamp", "id"),)

    id: int | None = Field(default=None, primary_key=True)
    entity_type: str = Field(index=True)
    entity_id: int
    field: str
    old_value: str | None = Field(default=None)
    new_value: str | None = Field(default=None)
    user_id: int = Field(foreign_key="users.id", index=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

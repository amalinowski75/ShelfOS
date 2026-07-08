"""Generic audit log (spec §19, decision D9).

A single table records field-level changes across entities (components,
invoices, parameters, locations).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    field: str
    old_value: str | None = Field(default=None)
    new_value: str | None = Field(default=None)
    user_id: int = Field(foreign_key="users.id")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

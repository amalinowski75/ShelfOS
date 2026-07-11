"""Audit logging service (spec §19, decision D9).

A single generic ``audit_log`` table records field-level changes across
entities: quantity, location, invoice and parameter modifications. Entries are
added to the caller's open transaction (no commit of their own), so an audit row
persists atomically with the change that produced it -- either both land or
neither does.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.audit import AuditLog


def record_change(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    field: str,
    old_value: Any,
    new_value: Any,
    user_id: int,
) -> None:
    """Append a field-level change to the audit log (no commit).

    ``old_value``/``new_value`` are coerced to text (``None`` stays ``None``) so
    the log can hold heterogeneous values in one schema (D9).
    """
    session.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            old_value=_as_text(old_value),
            new_value=_as_text(new_value),
            user_id=user_id,
        )
    )


def list_entries(
    session: Session,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    """Return audit entries, most recent first, optionally filtered by entity."""
    statement = select(AuditLog)
    if entity_type is not None:
        statement = statement.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == entity_id)
    statement = statement.order_by(
        col(AuditLog.timestamp).desc(), col(AuditLog.id).desc()
    ).limit(limit)
    return list(session.exec(statement).all())


def _as_text(value: Any) -> str | None:
    """Render an audited value as text, preserving ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

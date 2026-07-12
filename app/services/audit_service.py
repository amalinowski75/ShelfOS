"""Audit logging service (spec §19, decision D9).

A single generic ``audit_log`` table records field-level changes across
entities: quantity, location, invoice and parameter modifications. Entries are
added to the caller's open transaction (no commit of their own), so an audit row
persists atomically with the change that produced it -- either both land or
neither does.
"""

from __future__ import annotations

from typing import Any, Final

from sqlmodel import Session, col, select

from app.models.audit import AuditLog

# Canonical ``field`` names, kept here so producers and consumers of the log
# share one vocabulary instead of scattering literals across services (spec
# §19). Some fields are parameterized (a parameter name, a location id); those
# are built by the helpers below rather than hardcoded, and parsed back by their
# ``*_of`` counterparts so no consumer has to reinvent the encoding.
FIELD_DELETED: Final = "deleted"
FIELD_LOCATION_ID: Final = "location_id"
FIELD_IS_FINALIZED: Final = "is_finalized"
FIELD_TOTAL_GROSS: Final = "total_gross"

_PARAMETER_PREFIX: Final = "parameter:"
_QUANTITY_PREFIX: Final = "quantity@location:"


def parameter_field(definition_name: str) -> str:
    """Field name for an EAV parameter change (``parameter:<name>``)."""
    return f"{_PARAMETER_PREFIX}{definition_name}"


def quantity_field(location_id: int) -> str:
    """Field name for a stock change at a location (``quantity@location:<id>``)."""
    return f"{_QUANTITY_PREFIX}{location_id}"


def quantity_location_of(field: str) -> int | None:
    """Extract the location id from a quantity field, or ``None`` if not one."""
    if not field.startswith(_QUANTITY_PREFIX):
        return None
    try:
        return int(field[len(_QUANTITY_PREFIX) :])
    except ValueError:
        return None


def parameter_name_of(field: str) -> str | None:
    """Extract the definition name from a parameter field, or ``None`` if not one."""
    if not field.startswith(_PARAMETER_PREFIX):
        return None
    return field[len(_PARAMETER_PREFIX) :]


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

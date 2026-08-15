"""Audit logging service (spec §19, decision D9).

A single generic ``audit_log`` table records field-level changes across
entities: quantity, location, invoice and parameter modifications. Entries are
added to the caller's open transaction (no commit of their own), so an audit row
persists atomically with the change that produced it -- either both land or
neither does.

What is covered, as of now: stock quantities, components and their parameters,
invoices, their lines, their staged import lines, and locations. Five services
still mutate without recording anything -- ``user_service`` (so "who granted
admin" is unanswerable), ``match_rule_service`` (whose rules silently change how
every later import is read), ``link_service``, ``attachment_service`` and
``bom_service``. The list is here so the gaps are known rather than assumed
closed.

Creation is deliberately not audited anywhere: there is no prior value to record,
and the bulk location generator would otherwise write hundreds of rows saying
nothing but "this exists now". The consequence worth knowing is that no table
carries its creator, only its editors.

Producers take ``user_id`` in one of two shapes, and the difference is
deliberate: a service that is also called from a system context (import
materialisation, seeding) takes it optional and skips the entry when it is
absent, while one reachable only from a request takes it required, so a caller
that forgets it fails loudly instead of silently logging nothing.
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
# A component's editable scalar fields (admin edit, §12). Type and MPN are
# immutable, so they have no audit field.
FIELD_MANUFACTURER: Final = "manufacturer"
FIELD_PACKAGE: Final = "package"
FIELD_MOUNTING_TYPE: Final = "mounting_type"
FIELD_NOTES: Final = "notes"
# A staged import line under review (``invoice_import_line``): what the row will
# become is still being edited, so its identity fields are audited too — unlike a
# component's, which are immutable once created.
FIELD_TYPE_ID: Final = "type_id"
FIELD_MPN: Final = "mpn"
FIELD_DESCRIPTION: Final = "description"
FIELD_QUANTITY: Final = "quantity"
FIELD_PARAMETERS: Final = "parameters"
# A location's own fields (``location``). ``type`` is the location's kind (rack,
# shelf, …), not a component type — hence the distinct name from FIELD_TYPE_ID.
FIELD_NAME: Final = "name"
FIELD_PARENT_ID: Final = "parent_id"
FIELD_TYPE: Final = "type"

_PARAMETER_PREFIX: Final = "parameter:"
_QUANTITY_PREFIX: Final = "quantity@location:"
_IMPORT_LINE_PREFIX: Final = "import-line:"


def parameter_field(definition_name: str) -> str:
    """Field name for an EAV parameter change (``parameter:<name>``)."""
    return f"{_PARAMETER_PREFIX}{definition_name}"


def quantity_field(location_id: int) -> str:
    """Field name for a stock change at a location (``quantity@location:<id>``)."""
    return f"{_QUANTITY_PREFIX}{location_id}"


def import_line_field(line_no: int, field: str) -> str:
    """Field name for an edit to a staged import line (``import-line:<n>:<f>``).

    Staged rows are audited against their INVOICE, not against themselves. Their
    own ids are reused: the table has a plain ``INTEGER PRIMARY KEY``, and every
    finalize deletes all of an invoice's staged rows, so the next import starts
    numbering where the last one stopped -- two unrelated rows would share one
    history with nothing to separate them. ``(invoice_id, line_no)`` is stable,
    means something on the printed invoice, and outlives the row.
    """
    return f"{_IMPORT_LINE_PREFIX}{line_no}:{field}"


def import_line_of(field: str) -> tuple[int, str] | None:
    """Split an import-line field into ``(line_no, field)``, or ``None``.

    A missing line number, a non-numeric one or an empty inner field are all
    malformed and yield ``None`` rather than a half-parsed value.
    """
    if not field.startswith(_IMPORT_LINE_PREFIX):
        return None
    line_no, separator, inner = field[len(_IMPORT_LINE_PREFIX) :].partition(":")
    if not separator or not inner:
        return None
    try:
        return int(line_no), inner
    except ValueError:
        return None


def quantity_location_of(field: str) -> int | None:
    """Extract the location id from a quantity field, or ``None`` if not one."""
    if not field.startswith(_QUANTITY_PREFIX):
        return None
    try:
        return int(field[len(_QUANTITY_PREFIX) :])
    except ValueError:
        return None


def parameter_name_of(field: str) -> str | None:
    """Extract the definition name from a parameter field, or ``None`` if not one.

    A bare ``"parameter:"`` with no name is not a valid parameter field and
    yields ``None`` (mirroring ``quantity_location_of`` on an empty id).
    """
    if not field.startswith(_PARAMETER_PREFIX):
        return None
    return field[len(_PARAMETER_PREFIX) :] or None


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

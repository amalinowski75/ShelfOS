"""Component, type and parameter business logic (spec §4-6, §13).

Key rules implemented here:

* Component types form a hierarchy and *inherit* parameter definitions from all
  ancestors (decision D3).
* Parameter values use controlled EAV: a value is stored in exactly one typed
  column chosen by the definition's ``data_type`` (decision D6), and ``enum``
  values are validated against the allowed set.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.component import (
    Component,
    ComponentParameter,
    ComponentType,
    ParameterDefinition,
    ParameterEnumValue,
)
from app.models.enums import MatchDomain, MountingType, ParameterDataType
from app.models.invoice import InvoiceImportLine
from app.models.location import ComponentLocation
from app.models.match_rule import MatchRule
from app.services import attachment_service, audit_service, link_service
from app.services._common import require_entity
from app.services.errors import DuplicateComponentError, ValidationError
from app.units import UnitParseError, parse_engineering

ParameterValue = float | int | str | bool


def _coerce_number(value: ParameterValue, definition: ParameterDefinition) -> float:
    """Return a base-unit float for a NUMBER parameter value.

    A string is read as engineering notation (so ``"4k7"`` and ``"100 nF"``
    work); a raw int/float is taken as-is. ``bool`` is a subclass of ``int`` and
    is rejected explicitly. A non-finite value (``inf``/``nan``, which Pydantic
    accepts by default) is rejected so it never reaches storage or JSON.
    """
    if isinstance(value, bool):
        raise ValidationError(f"expected a number for {definition.name!r}")
    if isinstance(value, str):
        try:
            result = parse_engineering(value)
        except UnitParseError as exc:
            raise ValidationError(
                f"could not read {value!r} as a number for {definition.name!r}"
            ) from exc
    elif isinstance(value, int | float):
        result = float(value)
    else:
        raise ValidationError(f"expected a number for {definition.name!r}")

    if not math.isfinite(result):
        raise ValidationError(
            f"{value!r} is not a finite number for {definition.name!r}"
        )
    return result


@dataclass
class ParameterSpec:
    """One parameter definition to attach to a type (used for batch creation)."""

    name: str
    label: str
    data_type: ParameterDataType
    unit: str | None = None
    is_filterable: bool = False
    is_table_column: bool = False
    sort_order: int = 0
    enum_values: list[str] | None = field(default=None)


def create_type(
    session: Session, name: str, *, parent_id: int | None = None
) -> ComponentType:
    """Create a component type, optionally nested under a parent type."""
    if not name.strip():
        raise ValidationError("component type name must not be empty")
    if parent_id is not None:
        require_entity(session, ComponentType, parent_id, "component type")
    _require_unique_type_name(session, name, parent_id)
    ctype = ComponentType(name=name, parent_id=parent_id)
    session.add(ctype)
    session.commit()
    session.refresh(ctype)
    return ctype


def create_type_with_parameters(
    session: Session,
    name: str,
    *,
    parent_id: int | None = None,
    parameters: list[ParameterSpec] | None = None,
) -> ComponentType:
    """Create a type and all its parameter definitions in one transaction (§13).

    This is the convenient, atomic counterpart to calling :func:`create_type`
    followed by repeated :func:`add_parameter_definition`. Every parameter spec
    is validated *before* anything is written, so a bad spec leaves no partial
    type behind. Parameter ``name`` values must be unique within the batch.
    """
    specs = parameters or []
    if not name.strip():
        raise ValidationError("component type name must not be empty")
    if parent_id is not None:
        require_entity(session, ComponentType, parent_id, "component type")
    _require_unique_type_name(session, name, parent_id)

    seen: set[str] = set()
    for spec in specs:
        _validate_parameter_spec(spec)
        if spec.name in seen:
            raise ValidationError(f"duplicate parameter name {spec.name!r}")
        seen.add(spec.name)

    ctype = ComponentType(name=name, parent_id=parent_id)
    session.add(ctype)
    session.flush()  # assign ctype.id for the parameter foreign keys
    for spec in specs:
        _create_parameter_definition(session, cast(int, ctype.id), spec)
    session.commit()
    session.refresh(ctype)
    return ctype


def get_ancestry(session: Session, type_id: int) -> list[ComponentType]:
    """Return the type chain from root to the given type (inclusive).

    Raises:
        NotFoundError: If the type does not exist.
        ValidationError: If the parent chain contains a cycle.
    """
    chain: list[ComponentType] = []
    seen: set[int] = set()
    current: int | None = type_id
    while current is not None:
        if current in seen:
            raise ValidationError(f"cycle detected in type hierarchy at {current}")
        seen.add(current)
        ctype = require_entity(session, ComponentType, current, "component type")
        chain.append(ctype)
        current = ctype.parent_id
    chain.reverse()
    return chain


def add_parameter_definition(
    session: Session,
    type_id: int,
    *,
    name: str,
    label: str,
    data_type: ParameterDataType,
    unit: str | None = None,
    is_filterable: bool = False,
    is_table_column: bool = False,
    sort_order: int = 0,
    enum_values: list[str] | None = None,
) -> ParameterDefinition:
    """Define a parameter for a type (and its allowed enum values if any)."""
    require_entity(session, ComponentType, type_id, "component type")
    spec = ParameterSpec(
        name=name,
        label=label,
        data_type=data_type,
        unit=unit,
        is_filterable=is_filterable,
        is_table_column=is_table_column,
        sort_order=sort_order,
        enum_values=enum_values,
    )
    _validate_parameter_spec(spec)
    _require_unique_parameter_name(session, type_id, name)
    definition = _create_parameter_definition(session, type_id, spec)
    session.commit()
    session.refresh(definition)
    return definition


def count_components_of_type(session: Session, type_id: int) -> int:
    """How many live (non-deleted) components have this exact type (§13 edit)."""
    return session.exec(
        select(func.count())
        .select_from(Component)
        .where(Component.type_id == type_id, col(Component.deleted_at).is_(None))
    ).one()


def count_child_types(session: Session, type_id: int) -> int:
    """How many types name this one as their parent (blocks a delete)."""
    return session.exec(
        select(func.count())
        .select_from(ComponentType)
        .where(ComponentType.parent_id == type_id)
    ).one()


def count_import_lines_of_type(session: Session, type_id: int) -> int:
    """How many staged invoice-import lines reference this type (blocks a delete).

    ``InvoiceImportLine.type_id`` is a real FK, and a draft under review holds it
    long before any component exists. Deleting the type out from under it would
    leave a dangling reference that finalize rejects with a bare "component type N
    not found" — so a delete is blocked while such rows exist (mirrors how
    ``delete_location`` treats staged lines)."""
    return session.exec(
        select(func.count())
        .select_from(InvoiceImportLine)
        .where(InvoiceImportLine.type_id == type_id)
    ).one()


def _other_type_shares_name(session: Session, name: str, exclude_id: int) -> bool:
    """Does another type carry ``name`` (case-insensitively, as the engine folds it)?

    Type names are unique per parent, not globally, so ``name`` may belong to more
    than one type. The matching engine resolves a ``TYPE`` rule's target to a type by
    ``name.casefold()``, so whether renaming/deleting THIS type should touch the rules
    that name it depends on whether any OTHER type still answers to the same name.
    """
    folded = name.casefold()
    return any(
        t.id != exclude_id and t.name.casefold() == folded
        for t in session.exec(select(ComponentType)).all()
    )


def _type_rules_named(session: Session, name: str) -> list[MatchRule]:
    """The ``TYPE`` match rules whose target resolves to ``name``.

    Matched by ``casefold`` to mirror the engine (``matching._resolve_type`` does
    ``names.get(canonical.casefold())``); a rule stored as ``"Resistor"`` targets a
    type named ``"resistor"`` and must be found here too."""
    folded = name.casefold()
    return [
        rule
        for rule in session.exec(
            select(MatchRule).where(MatchRule.domain == MatchDomain.TYPE)
        ).all()
        if rule.canonical.casefold() == folded
    ]


def rename_type(
    session: Session, type_id: int, *, name: str, user_id: int | None = None
) -> ComponentType:
    """Rename a component type, keeping import matching consistent (§13 edit).

    Inventory references a type by id, so a rename is transparent there. The one
    place the NAME matters is the matching engine: ``TYPE`` match rules resolve to a
    type by its name (``MatchRule.canonical``, case-insensitively), so a rule pointing
    at the old name is repointed at the new one in the same transaction — otherwise a
    scanned/invoiced category would stop resolving to this type. But only when the old
    name was unique: if ANOTHER type still answers to it, those rules keep resolving to
    that other type and are left alone. Root-level uniqueness is guarded in the service
    (the DB constraint can't, since NULL != NULL); the DB constraint is the concurrency
    backstop.
    """
    ctype = require_entity(session, ComponentType, type_id, "component type")
    new = name.strip()
    if not new:
        raise ValidationError("component type name must not be empty")
    if new == ctype.name:
        return ctype  # no-op; don't trip the duplicate guard on the type itself
    _require_unique_type_name(session, new, ctype.parent_id)
    old = ctype.name
    try:
        # Repoint the TYPE rules that named this type — but only if it was the sole
        # bearer of the old name; otherwise the rules still resolve to the other type.
        if not _other_type_shares_name(session, old, type_id):
            for rule in _type_rules_named(session, old):
                rule.canonical = new
                session.add(rule)
        ctype.name = new
        session.add(ctype)
        if user_id is not None:
            audit_service.record_change(
                session,
                entity_type="component_type",
                entity_id=type_id,
                field=audit_service.FIELD_NAME,
                old_value=old,
                new_value=new,
                user_id=user_id,
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(ctype)
    return ctype


def _delete_definition_and_dependents(
    session: Session, definition: ParameterDefinition
) -> None:
    """Delete a parameter definition with its enum values and scoped match rules.

    No commit, and no guarding of stored values — the caller owns that. Extracted for
    :func:`delete_type`, whose block on children AND live components is what makes it
    safe there (no component can hold a value for this type's own definitions). A later
    single-definition caller must instead check that no component holds a value for the
    definition itself before calling this.
    """
    for enum_value in session.exec(
        select(ParameterEnumValue).where(
            ParameterEnumValue.parameter_definition_id == definition.id
        )
    ).all():
        session.delete(enum_value)
    for rule in session.exec(
        select(MatchRule).where(MatchRule.parameter_definition_id == definition.id)
    ).all():
        session.delete(rule)
    session.delete(definition)


def delete_type(
    session: Session, type_id: int, *, user_id: int | None = None
) -> None:
    """Permanently delete a type, only when nothing depends on it (§13 edit).

    Refused (``ValidationError``) if the type has child types, any live component, or
    a staged invoice-import line, naming the count so the admin knows what to clear
    first. When unused it is safe to hard-delete: with no children and no direct
    components, no ``ComponentParameter`` can reference this type's own definitions
    (that would need a descendant type with a component), so its definitions, their
    enum values and scoped match rules go with it, plus the ``TYPE`` match rules that
    named it — but only the rules now left dead, i.e. when no other type still answers
    to the name. One transaction, rolled back on any failure.
    """
    ctype = require_entity(session, ComponentType, type_id, "component type")
    children = count_child_types(session, type_id)
    if children:
        raise ValidationError(
            f"{children} child types depend on this type; delete or move them first"
        )
    components = count_components_of_type(session, type_id)
    if components:
        raise ValidationError(
            f"{components} components use this type; reassign or delete them first"
        )
    import_lines = count_import_lines_of_type(session, type_id)
    if import_lines:
        raise ValidationError(
            f"{import_lines} staged invoice lines use this type; "
            "review or dismiss them first"
        )
    try:
        for definition in list_own_parameter_definitions(session, type_id):
            _delete_definition_and_dependents(session, definition)
        # Drop the TYPE rules that named this type only if it was the last to answer
        # to the name; otherwise they still resolve to the remaining same-named type.
        if not _other_type_shares_name(session, ctype.name, type_id):
            for rule in _type_rules_named(session, ctype.name):
                session.delete(rule)
        if user_id is not None:
            audit_service.record_change(
                session,
                entity_type="component_type",
                entity_id=type_id,
                field=audit_service.FIELD_DELETED,
                old_value=False,
                new_value=True,
                user_id=user_id,
            )
        session.delete(ctype)
        session.commit()
    except Exception:
        session.rollback()
        raise


def count_components_using_definition(session: Session, definition_id: int) -> int:
    """How many stored component values reference this parameter (blocks delete)."""
    return session.exec(
        select(func.count())
        .select_from(ComponentParameter)
        .where(ComponentParameter.parameter_definition_id == definition_id)
    ).one()


def _staged_line_parameters(
    session: Session,
) -> Iterable[tuple[InvoiceImportLine, dict[str, Any]]]:
    """Every ``(line, entry)`` in every staged invoice line's ``parameters`` JSON.

    ``InvoiceImportLine.parameters`` is a JSON list of
    ``{"parameter_definition_id": int, "value": ...}`` — NOT a real FK — that a draft
    under review holds before any component exists. Scanned in Python (the review
    panel is small) so a definition/token a draft still references can block its
    deletion, the way ``count_import_lines_of_type`` guards the type itself."""
    for line in session.exec(select(InvoiceImportLine)).all():
        for entry in line.parameters or []:
            if isinstance(entry, dict):
                yield line, entry


def count_import_lines_using_definition(session: Session, definition_id: int) -> int:
    """Distinct staged invoice lines whose parameters JSON names this definition."""
    return len(
        {
            line.id
            for line, entry in _staged_line_parameters(session)
            if entry.get("parameter_definition_id") == definition_id
        }
    )


def count_import_lines_using_enum_value(
    session: Session, definition_id: int, token: str
) -> int:
    """Distinct staged invoice lines holding ``token`` for this enum parameter."""
    return len(
        {
            line.id
            for line, entry in _staged_line_parameters(session)
            if entry.get("parameter_definition_id") == definition_id
            and entry.get("value") == token
        }
    )


def staged_definition_ids(session: Session) -> set[int]:
    """Every parameter-definition id any staged invoice line references.

    The batched counterpart of :func:`count_import_lines_using_definition`, for the
    /types page which needs the whole set at once — so the page's "deletable" flag
    and the DELETE endpoint read one description of what a staged line points at,
    rather than each re-walking the JSON and drifting apart."""
    return {
        pid
        for _, entry in _staged_line_parameters(session)
        if isinstance(pid := entry.get("parameter_definition_id"), int)
    }


def staged_type_counts(session: Session) -> dict[int, int]:
    """``{type_id: staged-line count}`` — the batched form of
    :func:`count_import_lines_of_type`, for the /types feed."""
    return {
        type_id: n
        for type_id, n in session.exec(
            select(InvoiceImportLine.type_id, func.count()).group_by(
                col(InvoiceImportLine.type_id)
            )
        ).all()
        if type_id is not None
    }


def count_components_using_enum_value(
    session: Session, definition_id: int, token: str
) -> int:
    """How many components hold ``token`` for this enum parameter (an enum token is
    stored in ``value_text``). Blocks removing that token."""
    return session.exec(
        select(func.count())
        .select_from(ComponentParameter)
        .where(
            ComponentParameter.parameter_definition_id == definition_id,
            ComponentParameter.value_text == token,
        )
    ).one()


def _audit_definition(
    session: Session,
    definition_id: int,
    field: str,
    old_value: object,
    new_value: object,
    user_id: int | None,
) -> None:
    """Record one parameter-definition change, when a user id is given (§19)."""
    if user_id is not None:
        audit_service.record_change(
            session,
            entity_type="parameter_definition",
            entity_id=definition_id,
            field=field,
            old_value=old_value,
            new_value=new_value,
            user_id=user_id,
        )


def _set_definition_scalar(
    session: Session,
    definition: ParameterDefinition,
    attr: str,
    new_value: object,
    field: str,
    user_id: int | None,
) -> None:
    """Set one scalar field of a definition, auditing the change (no-ops skipped)."""
    old_value = getattr(definition, attr)
    if old_value == new_value:
        return
    setattr(definition, attr, new_value)
    _audit_definition(
        session, cast(int, definition.id), field, old_value, new_value, user_id
    )


def _sync_enum_values(
    session: Session,
    definition: ParameterDefinition,
    new_values: list[str],
    user_id: int | None = None,
) -> None:
    """Reconcile an enum parameter's allowed tokens to ``new_values`` (no commit).

    Order matters (it is the display order), so this also renumbers ``sort_order``.
    A token that some component still holds — or that a staged invoice line still
    references — cannot be removed (``ValidationError`` with the count); a removed
    token's own ``ENUM_VALUE`` match rules go with it.
    """
    cleaned = _clean_enum_values(new_values)
    existing = session.exec(
        select(ParameterEnumValue)
        .where(ParameterEnumValue.parameter_definition_id == definition.id)
        .order_by(ParameterEnumValue.sort_order, ParameterEnumValue.id)  # type: ignore[arg-type]
    ).all()
    old_values = [row.value for row in existing]
    if cleaned == old_values:
        return  # nothing changed, order included
    kept = {row.value: row for row in existing}
    keep = set(cleaned)
    for row in existing:
        if row.value in keep:
            continue
        used = count_components_using_enum_value(
            session, cast(int, definition.id), row.value
        )
        if used:
            raise ValidationError(
                f"{used} components use value {row.value!r}; cannot remove it"
            )
        staged = count_import_lines_using_enum_value(
            session, cast(int, definition.id), row.value
        )
        if staged:
            raise ValidationError(
                f"{staged} staged invoice lines use value {row.value!r}; "
                "review or dismiss them first"
            )
        for rule in session.exec(
            select(MatchRule).where(
                MatchRule.domain == MatchDomain.ENUM_VALUE,
                MatchRule.parameter_definition_id == definition.id,
                MatchRule.canonical == row.value,
            )
        ).all():
            session.delete(rule)
        session.delete(row)
    for order, value in enumerate(cleaned):
        existing_row = kept.get(value)
        if existing_row is None:
            session.add(
                ParameterEnumValue(
                    parameter_definition_id=definition.id, value=value, sort_order=order
                )
            )
        else:
            existing_row.sort_order = order
            session.add(existing_row)
    _audit_definition(
        session,
        cast(int, definition.id),
        audit_service.FIELD_ENUM_VALUES,
        ", ".join(old_values),
        ", ".join(cleaned),
        user_id,
    )


def update_parameter_definition(
    session: Session,
    definition_id: int,
    *,
    name: str | None = None,
    label: str | None = None,
    unit: str | None = None,
    sort_order: int | None = None,
    is_table_column: bool | None = None,
    is_filterable: bool | None = None,
    enum_values: list[str] | None = None,
    user_id: int | None = None,
) -> ParameterDefinition:
    """Edit a parameter definition in one transaction (§13 edit).

    A partial PATCH: every field is optional and ``None`` means "leave unchanged",
    so a caller can send just the one field it edits without wiping the others. (To
    clear the ``unit``, send an empty string, not null.) ``data_type`` is immutable
    and not accepted here — changing it would invalidate values already stored under
    the old type. A ``name`` rename repoints the ``PARAM_NAME`` match rules scoped to
    this definition (consistency — the engine keys them by the stable id); the enum
    tokens are reconciled (a token a component or a staged invoice line still holds
    cannot be removed). Any failure rolls the whole edit back.
    """
    definition = require_entity(
        session, ParameterDefinition, definition_id, "parameter definition"
    )
    is_enum = definition.data_type is ParameterDataType.ENUM
    if enum_values is not None and not is_enum:
        raise ValidationError("enum_values only apply to enum parameters")
    try:
        if name is not None:
            new_name = name.strip()
            if not new_name:
                raise ValidationError("parameter name must not be empty")
            if new_name != definition.name:
                _require_unique_parameter_name(session, definition.type_id, new_name)
                old_name = definition.name
                for rule in session.exec(
                    select(MatchRule).where(
                        MatchRule.domain == MatchDomain.PARAM_NAME,
                        MatchRule.parameter_definition_id == definition_id,
                    )
                ).all():
                    rule.canonical = new_name
                    session.add(rule)
                _audit_definition(
                    session, definition_id, audit_service.FIELD_NAME, old_name,
                    new_name, user_id,
                )
                definition.name = new_name
        if label is not None:
            new_label = label.strip()
            if not new_label:
                raise ValidationError("parameter label must not be empty")
            _set_definition_scalar(
                session, definition, "label", new_label,
                audit_service.FIELD_LABEL, user_id,
            )
        if unit is not None:
            _set_definition_scalar(
                session, definition, "unit", _blank_to_none(unit),
                audit_service.FIELD_UNIT, user_id,
            )
        if sort_order is not None:
            _set_definition_scalar(
                session, definition, "sort_order", sort_order,
                audit_service.FIELD_SORT_ORDER, user_id,
            )
        if is_table_column is not None:
            _set_definition_scalar(
                session, definition, "is_table_column", is_table_column,
                audit_service.FIELD_IS_TABLE_COLUMN, user_id,
            )
        if is_filterable is not None:
            _set_definition_scalar(
                session, definition, "is_filterable", is_filterable,
                audit_service.FIELD_IS_FILTERABLE, user_id,
            )
        session.add(definition)
        if enum_values is not None:
            _sync_enum_values(session, definition, enum_values, user_id)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(definition)
    return definition


def delete_parameter_definition(
    session: Session, definition_id: int, *, user_id: int | None = None
) -> None:
    """Delete a parameter definition, only when nothing holds a value for it (§13 edit).

    Refused (``ValidationError``) with a count if any component holds a value for it,
    or a staged invoice line still references it (its ``parameters`` JSON is not a
    real FK, but finalize would reject the dangling id); otherwise its enum values and
    scoped match rules go with it. Transactional.
    """
    definition = require_entity(
        session, ParameterDefinition, definition_id, "parameter definition"
    )
    used = count_components_using_definition(session, definition_id)
    if used:
        raise ValidationError(
            f"{used} components have a value for this parameter; clear them first"
        )
    staged = count_import_lines_using_definition(session, definition_id)
    if staged:
        raise ValidationError(
            f"{staged} staged invoice lines use this parameter; "
            "review or dismiss them first"
        )
    try:
        if user_id is not None:
            audit_service.record_change(
                session,
                entity_type="parameter_definition",
                entity_id=definition_id,
                field=audit_service.FIELD_DELETED,
                old_value=False,
                new_value=True,
                user_id=user_id,
            )
        _delete_definition_and_dependents(session, definition)
        session.commit()
    except Exception:
        session.rollback()
        raise


def _require_unique_type_name(
    session: Session, name: str, parent_id: int | None
) -> None:
    """Reject a type name already used among a parent's direct children."""
    existing = session.exec(
        select(ComponentType.id).where(
            ComponentType.name == name,
            col(ComponentType.parent_id).is_(parent_id)
            if parent_id is None
            else ComponentType.parent_id == parent_id,
        )
    ).first()
    if existing is not None:
        raise ValidationError(
            f"a component type named {name!r} already exists under this parent"
        )


def _require_unique_parameter_name(
    session: Session, type_id: int, name: str
) -> None:
    """Reject a parameter name already defined directly on the type."""
    existing = session.exec(
        select(ParameterDefinition.id).where(
            ParameterDefinition.type_id == type_id,
            ParameterDefinition.name == name,
        )
    ).first()
    if existing is not None:
        raise ValidationError(
            f"parameter {name!r} is already defined on this type"
        )


def _clean_enum_values(values: list[str] | None) -> list[str]:
    """Validate and normalise enum tokens: stripped, non-blank, at least one, and
    unique IGNORING CASE.

    One normaliser for both create and edit so they can't drift — a token stored
    unstripped by one and stripped by the other reads as a remove+add and can lock
    an in-use definition. Case-folded uniqueness matches how the vocabulary is
    consumed (``_canonical_target`` resolves an ``ENUM_VALUE`` rule by ``casefold``
    and returns the first match), so ``"Flat"``/``"flat"`` would leave the second
    untargetable. The stripped list is returned for the caller to store."""
    cleaned = [value.strip() for value in (values or [])]
    if not cleaned:
        raise ValidationError("enum parameters require at least one allowed value")
    if any(not value for value in cleaned):
        raise ValidationError("enum values must not be blank")
    if len({value.casefold() for value in cleaned}) != len(cleaned):
        raise ValidationError("enum values must be unique (case-insensitive)")
    return cleaned


def _validate_parameter_spec(spec: ParameterSpec) -> None:
    """Check a parameter spec against the EAV/enum rules (decision D6)."""
    if not spec.name.strip():
        raise ValidationError("parameter name must not be empty")
    if spec.data_type is not ParameterDataType.ENUM and spec.enum_values:
        raise ValidationError("enum_values only apply to enum parameters")
    if spec.data_type is ParameterDataType.ENUM:
        _clean_enum_values(spec.enum_values)  # raises on blank/dup/empty


def _create_parameter_definition(
    session: Session, type_id: int, spec: ParameterSpec
) -> ParameterDefinition:
    """Add a definition and its enum values to the session (no commit).

    The spec must already be validated. The definition is flushed so its ``id``
    is available for the enum-value foreign keys.
    """
    definition = ParameterDefinition(
        type_id=type_id,
        name=spec.name,
        label=spec.label,
        data_type=spec.data_type,
        unit=spec.unit,
        is_filterable=spec.is_filterable,
        is_table_column=spec.is_table_column,
        sort_order=spec.sort_order,
    )
    session.add(definition)
    session.flush()
    # Store the STRIPPED tokens so create and edit agree on what a token is (see
    # _clean_enum_values); the spec was validated, so this won't raise.
    tokens = (
        _clean_enum_values(spec.enum_values)
        if spec.data_type is ParameterDataType.ENUM
        else []
    )
    for order, value in enumerate(tokens):
        session.add(
            ParameterEnumValue(
                parameter_definition_id=definition.id,
                value=value,
                sort_order=order,
            )
        )
    return definition


def list_own_parameter_definitions(
    session: Session, type_id: int
) -> list[ParameterDefinition]:
    """Return only the parameter definitions declared directly on a type.

    Unlike :func:`get_effective_parameter_definitions`, this excludes inherited
    definitions — useful for confirming what a freshly created type owns (§13).
    """
    require_entity(session, ComponentType, type_id, "component type")
    return list(
        session.exec(
            select(ParameterDefinition)
            .where(ParameterDefinition.type_id == type_id)
            .order_by(ParameterDefinition.sort_order, ParameterDefinition.id)  # type: ignore[arg-type]
        ).all()
    )


def get_effective_parameter_definitions(
    session: Session, type_id: int
) -> list[ParameterDefinition]:
    """Return all parameter definitions visible for a type.

    The effective set is the union of definitions along the whole path to the
    root, ordered ancestor-first and then by ``sort_order`` (decision D3).
    """
    definitions: list[ParameterDefinition] = []
    for ctype in get_ancestry(session, type_id):
        rows = session.exec(
            select(ParameterDefinition)
            .where(ParameterDefinition.type_id == ctype.id)
            .order_by(ParameterDefinition.sort_order, ParameterDefinition.id)  # type: ignore[arg-type]
        ).all()
        definitions.extend(rows)
    return definitions


def enum_values_of(session: Session, definition_id: int) -> list[str]:
    """Return an enum parameter's allowed tokens in display order (decision D6).

    Non-enum definitions simply have none, so this returns an empty list.
    """
    return list(
        session.exec(
            select(col(ParameterEnumValue.value))
            .where(ParameterEnumValue.parameter_definition_id == definition_id)
            .order_by(
                ParameterEnumValue.sort_order,  # type: ignore[arg-type]
                ParameterEnumValue.id,  # type: ignore[arg-type]
            )
        ).all()
    )


def enum_values_by_definition(
    session: Session, definition_ids: Iterable[int]
) -> dict[int, list[str]]:
    """Batch-load allowed enum tokens for many definitions in one query.

    Returns ``{definition_id: [values in display order]}`` with only enum
    definitions present. Fetching every definition's values at once avoids an
    N+1 when rendering a whole parameter set (e.g. the effective set for a type).
    """
    ids = list(definition_ids)
    if not ids:
        return {}
    rows = session.exec(
        select(
            col(ParameterEnumValue.parameter_definition_id),
            col(ParameterEnumValue.value),
        )
        .where(col(ParameterEnumValue.parameter_definition_id).in_(ids))
        .order_by(
            ParameterEnumValue.sort_order,  # type: ignore[arg-type]
            ParameterEnumValue.id,  # type: ignore[arg-type]
        )
    ).all()
    grouped: dict[int, list[str]] = {}
    for definition_id, value in rows:
        grouped.setdefault(definition_id, []).append(value)
    return grouped


def create_component(
    session: Session,
    type_id: int,
    *,
    manufacturer: str | None = None,
    mpn: str | None = None,
    package: str | None = None,
    mounting_type: MountingType = MountingType.OTHER,
    notes: str | None = None,
) -> Component:
    """Create a component of the given type (no initial parameter values)."""
    return create_component_with_values(
        session,
        type_id,
        manufacturer=manufacturer,
        mpn=mpn,
        package=package,
        mounting_type=mounting_type,
        notes=notes,
    )


def create_component_with_values(
    session: Session,
    type_id: int,
    *,
    manufacturer: str | None = None,
    mpn: str | None = None,
    package: str | None = None,
    mounting_type: MountingType = MountingType.OTHER,
    notes: str | None = None,
    values: Iterable[tuple[int, ParameterValue]] = (),
    user_id: int | None = None,
    commit: bool = True,
) -> Component:
    """Create a component and its initial parameter values in one transaction (§16.5).

    Each ``(parameter_definition_id, value)`` must reference a definition in the
    type's effective set (own + inherited, D3); a value that fails validation
    (unknown/duplicate definition, wrong type, unparseable number) aborts the
    whole create, so a component is never left half-populated. When ``user_id``
    is given each initial value is recorded in the audit log (§19), matching the
    later ``set_parameter_value`` path.

    ``commit=False`` leaves the new component flushed (its id assigned) but uncommitted
    in the caller's transaction — for finalize, which creates several components and the
    invoice's stock in one all-or-nothing commit. The caller then owns rollback.
    """
    require_entity(session, ComponentType, type_id, "component type")
    component = Component(
        type_id=type_id,
        # Normalise so blanks and surrounding whitespace never reach the store: a
        # stray " " or "" would otherwise slip past the (MPN, manufacturer) de-dup
        # (which compares trimmed) and confuse case/whitespace-sensitive lookups.
        manufacturer=_blank_to_none(manufacturer),
        mpn=_blank_to_none(mpn),
        package=package,
        mounting_type=mounting_type,
        notes=notes,
    )
    pairs = list(values)
    session.add(component)
    try:
        session.flush()  # assign component.id without ending the transaction

        # Only resolve the effective parameter set when there is something to
        # apply, so the common no-parameters create adds no extra queries.
        definitions = (
            {d.id: d for d in get_effective_parameter_definitions(session, type_id)}
            if pairs
            else {}
        )
        # Batch-load allowed enum tokens for the enum definitions being set in one
        # query, so validating K enum values costs one query, not K. Restrict to
        # known enum definitions so an all-non-enum create issues no enum query at
        # all (and foreign/unknown ids stay out of the IN clause).
        enum_ids = [
            definition_id
            for definition_id, _ in pairs
            if (d := definitions.get(definition_id)) is not None
            and d.data_type is ParameterDataType.ENUM
        ]
        allowed_enums = {
            definition_id: set(values)
            for definition_id, values in enum_values_by_definition(
                session, enum_ids
            ).items()
        }
        seen: set[int] = set()
        for definition_id, value in pairs:
            if definition_id in seen:
                raise ValidationError(
                    f"parameter definition {definition_id} given more than once"
                )
            seen.add(definition_id)
            definition = definitions.get(definition_id)
            if definition is None:
                raise ValidationError(
                    "parameter definition does not apply to this component's type"
                )
            param = ComponentParameter(
                component_id=cast(int, component.id),
                parameter_definition_id=definition_id,
            )
            _assign_value(
                session,
                param,
                definition,
                value,
                # For an enum definition with no rows this is an empty set, which
                # correctly rejects any token (an enum always has ≥1 allowed value).
                allowed_enum=allowed_enums.get(definition_id, set())
                if definition.data_type is ParameterDataType.ENUM
                else None,
            )
            session.add(param)
            if user_id is not None:
                audit_service.record_change(
                    session,
                    entity_type="component",
                    entity_id=cast(int, component.id),
                    field=audit_service.parameter_field(definition.name),
                    old_value=None,
                    new_value=_current_value(param),
                    user_id=user_id,
                )

        if commit:
            session.commit()
    except Exception:
        # Roll back the flushed component so a bad value never leaves a
        # half-populated row behind — but only when we own the transaction; with
        # commit=False the caller manages (and will roll back) the whole unit.
        if commit:
            session.rollback()
        raise

    if commit:
        session.refresh(component)
    return component


def list_components(session: Session, *, type_id: int | None = None) -> list[Component]:
    """List non-deleted components, optionally filtered to a single type (§11)."""
    statement = select(Component).where(col(Component.deleted_at).is_(None))
    if type_id is not None:
        statement = statement.where(Component.type_id == type_id)
    return list(session.exec(statement.order_by(col(Component.id))).all())


def find_components_by_mpn(session: Session, mpn: str) -> list[Component]:
    """Return non-deleted components matching an MPN (BOM import, §21).

    Case-insensitive, since a KiCad library field and the inventory entry (and
    vendor catalogs) often differ only in case. MPN is not unique, so this can
    return several; the caller decides how to use them (e.g. sum their stock).
    """
    return list(
        session.exec(
            select(Component)
            .where(func.lower(col(Component.mpn)) == mpn.lower())
            .where(col(Component.deleted_at).is_(None))
            .order_by(col(Component.id))
        ).all()
    )


def _blank_to_none(text: str | None) -> str | None:
    """A trimmed non-empty string, or None for a blank/whitespace-only value."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def _match_key(text: str | None) -> str:
    """A trimmed, case-folded key for duplicate matching (``None`` → ``""``)."""
    return (text or "").strip().casefold()


def find_duplicate_component(
    session: Session, *, mpn: str | None, manufacturer: str | None
) -> Component | None:
    """The existing non-deleted component with the same (MPN, manufacturer), or None.

    Both are matched case-insensitively and whitespace-insensitively. A blank MPN is
    exempt: a part with no MPN can't be de-duplicated (generic passives, BOM lines
    without a part number), and two such parts must be allowed to coexist. The
    manufacturer is matched null-aware — a blank manufacturer matches only other
    blank ones (NULL *or* a legacy empty string) — so the same MPN from two different
    manufacturers is not a duplicate.

    Normalisation is done in Python on both sides, deliberately not in SQL: SQLite's
    built-in ``lower()``/``trim()`` are ASCII- and space-only, so a non-ASCII
    manufacturer ("ÉCLAIR" vs "éclair") or a tab/NBSP-padded legacy value would
    escape a DB-folded comparison while a Python one catches it. This runs once per
    create (not per row of a listing), so scanning the component table is fine.
    """
    if _blank_to_none(mpn) is None:
        return None
    mpn_key, mfr_key = _match_key(mpn), _match_key(manufacturer)
    candidates = session.exec(
        select(Component)
        .where(col(Component.deleted_at).is_(None))
        .where(col(Component.mpn).is_not(None))
        .order_by(col(Component.id))
    )
    for component in candidates:
        if (
            _match_key(component.mpn) == mpn_key
            and _match_key(component.manufacturer) == mfr_key
        ):
            return component
    return None


def list_types(session: Session) -> list[ComponentType]:
    """List all component types ordered by name (for the type filter, §11)."""
    return list(
        session.exec(select(ComponentType).order_by(col(ComponentType.name))).all()
    )


def list_parameter_values(
    session: Session, component_id: int
) -> list[ComponentParameter]:
    """Return all stored EAV values for a component."""
    require_entity(session, Component, component_id, "component")
    return list(
        session.exec(
            select(ComponentParameter).where(
                ComponentParameter.component_id == component_id
            )
        ).all()
    )


def hard_delete_component(
    session: Session, component_id: int, *, user_id: int | None = None
) -> None:
    """Permanently delete a component and its EAV/stock rows (admin only, §20).

    This is the administrative delete exposed through the backend API; the normal
    UI never deletes components. Related stock movements and invoice lines are
    left untouched as historical records. When ``user_id`` is given the deletion
    is recorded in the audit log (spec §19) within the same transaction.
    """
    component = require_entity(session, Component, component_id, "component")
    if user_id is not None:
        audit_service.record_change(
            session,
            entity_type="component",
            entity_id=component_id,
            field=audit_service.FIELD_DELETED,
            old_value=False,
            new_value=True,
            user_id=user_id,
        )
    for param in list_parameter_values(session, component_id):
        session.delete(param)
    for cl in session.exec(
        select(ComponentLocation).where(ComponentLocation.component_id == component_id)
    ).all():
        session.delete(cl)
    # Neither attachments nor links have an FK cascade — clean both here so a hard
    # delete leaves nothing orphaned (§10, §20).
    attachment_service.delete_attachments_for(
        session, entity_type="component", entity_id=component_id
    )
    link_service.delete_links_for(
        session, entity_type="component", entity_id=component_id
    )
    session.delete(component)
    session.commit()


def set_parameter_value(
    session: Session,
    component_id: int,
    parameter_definition_id: int,
    value: ParameterValue,
    *,
    user_id: int | None = None,
) -> ComponentParameter:
    """Set (or update) an EAV parameter value with type/enum validation.

    The definition must be part of the component type's effective set, enforcing
    parameter inheritance (decision D3). The value is routed to the column that
    matches the definition's ``data_type`` (decision D6). When ``user_id`` is
    given the change is recorded in the audit log (spec §19).
    """
    component = require_entity(session, Component, component_id, "component")
    definition = require_entity(
        session, ParameterDefinition, parameter_definition_id, "parameter definition"
    )

    valid_ids = {
        d.id for d in get_effective_parameter_definitions(session, component.type_id)
    }
    if definition.id not in valid_ids:
        raise ValidationError(
            "parameter definition does not apply to this component's type"
        )

    param = session.exec(
        select(ComponentParameter).where(
            ComponentParameter.component_id == component_id,
            ComponentParameter.parameter_definition_id == parameter_definition_id,
        )
    ).first() or ComponentParameter(
        component_id=component_id,
        parameter_definition_id=parameter_definition_id,
    )

    old_value = _current_value(param)
    _assign_value(session, param, definition, value)
    new_value = _current_value(param)
    # Log the normalized stored value (e.g. int 4700 -> 4700.0), so a value
    # renders identically whether it is read back as new_value here or as the
    # next change's old_value via _current_value. Skip no-op updates (the same
    # value set again) so they do not clutter the log with phantom changes.
    if user_id is not None and new_value != old_value:
        audit_service.record_change(
            session,
            entity_type="component",
            entity_id=component_id,
            field=audit_service.parameter_field(definition.name),
            old_value=old_value,
            new_value=new_value,
            user_id=user_id,
        )
    session.add(param)
    session.commit()
    session.refresh(param)
    return param


def _apply_scalar(
    session: Session,
    component: Component,
    attr: str,
    new_value: str | None,
    field: str,
    user_id: int | None,
) -> None:
    """Set one scalar component field, auditing the change (no-ops skipped)."""
    old_value = getattr(component, attr)
    if old_value == new_value:
        return
    setattr(component, attr, new_value)
    if user_id is not None:
        audit_service.record_change(
            session,
            entity_type="component",
            entity_id=cast(int, component.id),
            field=field,
            old_value=old_value,
            new_value=new_value,
            user_id=user_id,
        )


def _is_blank(value: ParameterValue | None) -> bool:
    """A parameter edit value that means 'clear' — None or an all-whitespace string."""
    return value is None or (isinstance(value, str) and not value.strip())


def update_component(
    session: Session,
    component_id: int,
    *,
    manufacturer: str | None = None,
    package: str | None = None,
    mounting_type: MountingType = MountingType.OTHER,
    notes: str | None = None,
    values: Iterable[tuple[int, ParameterValue | None]] = (),
    user_id: int | None = None,
) -> Component:
    """Edit a component's mutable fields and parameter values (admin only, §12).

    Type and MPN are immutable and are not accepted here. The scalar fields and the
    parameter values are updated in ONE transaction — a bad value aborts the whole
    edit, so the component is never left half-changed. A blank/None parameter value
    clears (deletes) that value; each parameter definition must be in the type's
    effective set (D3). Every change is recorded in the audit log (§19) when
    ``user_id`` is given, matching the create/set-value paths.

    Concurrency is last-write-wins: there is no version check, so two admins editing
    the same component at once means the later Save overwrites the earlier one
    (consistent with the create-race stance). Acceptable for this app's single-writer
    use. ``require_entity`` intentionally fetches by id without a ``deleted_at``
    filter — no soft-delete path exists today (only ``hard_delete_component``); if one
    is ever added, guard against editing a deleted component here.
    """
    component = require_entity(session, Component, component_id, "component")
    pairs = list(values)
    # MPN is immutable but manufacturer is editable, so an edit can still collide with
    # another part — apply the same (MPN, manufacturer) guard as create, excluding
    # this component itself. Reuses DuplicateComponentError so the dialog links to the
    # existing part just as the create flow does.
    new_manufacturer = _blank_to_none(manufacturer)
    duplicate = find_duplicate_component(
        session, mpn=component.mpn, manufacturer=new_manufacturer
    )
    if duplicate is not None and duplicate.id != component_id:
        origin = f" from {new_manufacturer}" if new_manufacturer else ""
        raise DuplicateComponentError(
            f"A component with MPN {component.mpn}{origin} already exists.",
            existing_id=cast(int, duplicate.id),
        )
    try:
        _apply_scalar(
            session, component, "manufacturer", new_manufacturer,
            audit_service.FIELD_MANUFACTURER, user_id,
        )
        _apply_scalar(
            session, component, "package", _blank_to_none(package),
            audit_service.FIELD_PACKAGE, user_id,
        )
        _apply_scalar(
            session, component, "notes", _blank_to_none(notes),
            audit_service.FIELD_NOTES, user_id,
        )
        if component.mounting_type != mounting_type:
            old_mount = component.mounting_type
            component.mounting_type = mounting_type
            if user_id is not None:
                audit_service.record_change(
                    session,
                    entity_type="component",
                    entity_id=component_id,
                    field=audit_service.FIELD_MOUNTING_TYPE,
                    old_value=old_mount.value,
                    new_value=mounting_type.value,
                    user_id=user_id,
                )
        session.add(component)

        definitions = {
            d.id: d
            for d in get_effective_parameter_definitions(session, component.type_id)
        }
        enum_ids = [
            definition_id
            for definition_id, value in pairs
            if not _is_blank(value)
            and (d := definitions.get(definition_id)) is not None
            and d.data_type is ParameterDataType.ENUM
        ]
        allowed_enums = {
            definition_id: set(tokens)
            for definition_id, tokens in enum_values_by_definition(
                session, enum_ids
            ).items()
        }
        existing = {
            p.parameter_definition_id: p
            for p in list_parameter_values(session, component_id)
        }
        seen: set[int] = set()
        for definition_id, value in pairs:
            if definition_id in seen:
                raise ValidationError(
                    f"parameter definition {definition_id} given more than once"
                )
            seen.add(definition_id)
            definition = definitions.get(definition_id)
            if definition is None:
                raise ValidationError(
                    "parameter definition does not apply to this component's type"
                )
            param = existing.get(definition_id)
            old_value = _current_value(param) if param is not None else None
            if _is_blank(value):
                new_value: ParameterValue | None = None
                if param is not None:
                    session.delete(param)
            else:
                if param is None:
                    param = ComponentParameter(
                        component_id=component_id,
                        parameter_definition_id=definition_id,
                    )
                _assign_value(
                    session, param, definition, cast(ParameterValue, value),
                    allowed_enum=allowed_enums.get(definition_id, set())
                    if definition.data_type is ParameterDataType.ENUM
                    else None,
                )
                new_value = _current_value(param)
                session.add(param)
            if user_id is not None and new_value != old_value:
                audit_service.record_change(
                    session,
                    entity_type="component",
                    entity_id=component_id,
                    field=audit_service.parameter_field(definition.name),
                    old_value=old_value,
                    new_value=new_value,
                    user_id=user_id,
                )

        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(component)
    return component


def _current_value(param: ComponentParameter) -> ParameterValue | None:
    """Return the currently populated EAV value of a parameter row, if any."""
    if param.value_num is not None:
        return param.value_num
    if param.value_bool is not None:
        return param.value_bool
    return param.value_text


def _assign_value(
    session: Session,
    param: ComponentParameter,
    definition: ParameterDefinition,
    value: ParameterValue,
    *,
    allowed_enum: set[str] | None = None,
) -> None:
    """Populate exactly the value column matching the definition's data type.

    ``allowed_enum`` lets a caller pass the definition's allowed enum tokens when
    it has already batch-loaded them (see ``create_component_with_values``),
    avoiding a per-value ``enum_values_of`` query; when ``None`` the set is
    fetched here, which is fine for the single-value ``set_parameter_value`` path.
    """
    param.value_num = None
    param.value_text = None
    param.value_bool = None

    match definition.data_type:
        case ParameterDataType.NUMBER:
            param.value_num = _coerce_number(value, definition)
        case ParameterDataType.BOOL:
            if not isinstance(value, bool):
                raise ValidationError(f"expected a bool for {definition.name!r}")
            param.value_bool = value
        case ParameterDataType.TEXT:
            if not isinstance(value, str):
                raise ValidationError(f"expected text for {definition.name!r}")
            param.value_text = value.strip()  # store trimmed, not as-typed
        case ParameterDataType.ENUM:
            if not isinstance(value, str):
                raise ValidationError(f"expected an enum token for {definition.name!r}")
            allowed = (
                allowed_enum
                if allowed_enum is not None
                else set(enum_values_of(session, cast(int, definition.id)))
            )
            if value not in allowed:
                raise ValidationError(
                    f"{value!r} is not an allowed value for {definition.name!r}"
                )
            param.value_text = value

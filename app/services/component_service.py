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
from typing import cast

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.component import (
    Component,
    ComponentParameter,
    ComponentType,
    ParameterDefinition,
    ParameterEnumValue,
)
from app.models.enums import MountingType, ParameterDataType
from app.models.location import ComponentLocation
from app.services import attachment_service, audit_service
from app.services._common import require_entity
from app.services.errors import ValidationError
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


def _validate_parameter_spec(spec: ParameterSpec) -> None:
    """Check a parameter spec against the EAV/enum rules (decision D6)."""
    if not spec.name.strip():
        raise ValidationError("parameter name must not be empty")
    if spec.data_type is not ParameterDataType.ENUM and spec.enum_values:
        raise ValidationError("enum_values only apply to enum parameters")
    if spec.data_type is ParameterDataType.ENUM:
        if not spec.enum_values:
            raise ValidationError("enum parameters require at least one allowed value")
        # These values are surfaced to clients as selectable tokens, so reject
        # blanks and duplicates rather than presenting an unusable picker.
        if any(not value.strip() for value in spec.enum_values):
            raise ValidationError("enum values must not be blank")
        if len(set(spec.enum_values)) != len(spec.enum_values):
            raise ValidationError("enum values must be unique")


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
    for order, value in enumerate(spec.enum_values or []):
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
) -> Component:
    """Create a component and its initial parameter values in one transaction (§16.5).

    Each ``(parameter_definition_id, value)`` must reference a definition in the
    type's effective set (own + inherited, D3); a value that fails validation
    (unknown/duplicate definition, wrong type, unparseable number) aborts the
    whole create, so a component is never left half-populated. When ``user_id``
    is given each initial value is recorded in the audit log (§19), matching the
    later ``set_parameter_value`` path.
    """
    require_entity(session, ComponentType, type_id, "component type")
    component = Component(
        type_id=type_id,
        manufacturer=manufacturer,
        mpn=mpn,
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

        session.commit()
    except Exception:
        # Roll back the flushed component so a bad value never leaves a
        # half-populated row behind, regardless of the caller's own handling.
        session.rollback()
        raise

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
    # The attachments table has no FK cascade — clean its rows + files here so a
    # hard delete leaves nothing orphaned (§10, §20).
    attachment_service.delete_attachments_for(
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
            param.value_text = value
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

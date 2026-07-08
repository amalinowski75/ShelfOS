"""Component, type and parameter business logic (spec §4-6, §13).

Key rules implemented here:

* Component types form a hierarchy and *inherit* parameter definitions from all
  ancestors (decision D3).
* Parameter values use controlled EAV: a value is stored in exactly one typed
  column chosen by the definition's ``data_type`` (decision D6), and ``enum``
  values are validated against the allowed set.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.component import (
    Component,
    ComponentParameter,
    ComponentType,
    ParameterDefinition,
    ParameterEnumValue,
)
from app.models.enums import MountingType, ParameterDataType
from app.models.location import ComponentLocation
from app.services._common import require_entity
from app.services.errors import ValidationError

ParameterValue = float | int | str | bool


def create_type(
    session: Session, name: str, *, parent_id: int | None = None
) -> ComponentType:
    """Create a component type, optionally nested under a parent type."""
    if not name.strip():
        raise ValidationError("component type name must not be empty")
    if parent_id is not None:
        require_entity(session, ComponentType, parent_id, "component type")
    ctype = ComponentType(name=name, parent_id=parent_id)
    session.add(ctype)
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
    if data_type is ParameterDataType.ENUM and not enum_values:
        raise ValidationError("enum parameters require at least one allowed value")
    if data_type is not ParameterDataType.ENUM and enum_values:
        raise ValidationError("enum_values only apply to enum parameters")

    definition = ParameterDefinition(
        type_id=type_id,
        name=name,
        label=label,
        data_type=data_type,
        unit=unit,
        is_filterable=is_filterable,
        is_table_column=is_table_column,
        sort_order=sort_order,
    )
    session.add(definition)
    session.commit()
    session.refresh(definition)

    for order, value in enumerate(enum_values or []):
        session.add(
            ParameterEnumValue(
                parameter_definition_id=definition.id,
                value=value,
                sort_order=order,
            )
        )
    session.commit()
    return definition


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
    """Create a component of the given type."""
    require_entity(session, ComponentType, type_id, "component type")
    component = Component(
        type_id=type_id,
        manufacturer=manufacturer,
        mpn=mpn,
        package=package,
        mounting_type=mounting_type,
        notes=notes,
    )
    session.add(component)
    session.commit()
    session.refresh(component)
    return component


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


def hard_delete_component(session: Session, component_id: int) -> None:
    """Permanently delete a component and its EAV/stock rows (admin only, §20).

    This is the administrative delete exposed through the backend API; the normal
    UI never deletes components. Related stock movements and invoice lines are
    left untouched as historical records.
    """
    component = require_entity(session, Component, component_id, "component")
    for param in list_parameter_values(session, component_id):
        session.delete(param)
    for cl in session.exec(
        select(ComponentLocation).where(ComponentLocation.component_id == component_id)
    ).all():
        session.delete(cl)
    session.delete(component)
    session.commit()


def set_parameter_value(
    session: Session,
    component_id: int,
    parameter_definition_id: int,
    value: ParameterValue,
) -> ComponentParameter:
    """Set (or update) an EAV parameter value with type/enum validation.

    The definition must be part of the component type's effective set, enforcing
    parameter inheritance (decision D3). The value is routed to the column that
    matches the definition's ``data_type`` (decision D6).
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

    _assign_value(session, param, definition, value)
    session.add(param)
    session.commit()
    session.refresh(param)
    return param


def _assign_value(
    session: Session,
    param: ComponentParameter,
    definition: ParameterDefinition,
    value: ParameterValue,
) -> None:
    """Populate exactly the value column matching the definition's data type."""
    param.value_num = None
    param.value_text = None
    param.value_bool = None

    match definition.data_type:
        case ParameterDataType.NUMBER:
            # bool is a subclass of int, so reject it explicitly.
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValidationError(f"expected a number for {definition.name!r}")
            param.value_num = float(value)
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
            allowed = {
                v.value
                for v in session.exec(
                    select(ParameterEnumValue).where(
                        ParameterEnumValue.parameter_definition_id == definition.id
                    )
                ).all()
            }
            if value not in allowed:
                raise ValidationError(
                    f"{value!r} is not an allowed value for {definition.name!r}"
                )
            param.value_text = value

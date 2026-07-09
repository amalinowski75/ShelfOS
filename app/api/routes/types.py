"""Component type and parameter-definition endpoints (spec §5-6, §13)."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    ParameterDefinitionCreate,
    TypeCreate,
    TypeWithParameters,
)
from app.models.component import ComponentType, ParameterDefinition
from app.services import component_service as cs

router = APIRouter(prefix="/api/types", tags=["types"])


@router.get("", response_model=list[ComponentType])
def list_types(session: Session = Depends(get_session)) -> list[ComponentType]:
    """List all component types (e.g. to pick a parent when creating one, §13)."""
    return cs.list_types(session)


@router.post("", response_model=TypeWithParameters, status_code=status.HTTP_201_CREATED)
def create_type(
    payload: TypeCreate, session: Session = Depends(get_session)
) -> TypeWithParameters:
    """Create a type and, optionally, its parameter definitions in one call (§13)."""
    specs = [
        cs.ParameterSpec(
            name=p.name,
            label=p.label,
            data_type=p.data_type,
            unit=p.unit,
            is_filterable=p.is_filterable,
            is_table_column=p.is_table_column,
            sort_order=p.sort_order,
            enum_values=p.enum_values,
        )
        for p in payload.parameters
    ]
    ctype = cs.create_type_with_parameters(
        session, payload.name, parent_id=payload.parent_id, parameters=specs
    )
    return TypeWithParameters(
        id=cast(int, ctype.id),
        name=ctype.name,
        parent_id=ctype.parent_id,
        parameters=cs.list_own_parameter_definitions(session, cast(int, ctype.id)),
    )


@router.get("/{type_id}/parameters", response_model=list[ParameterDefinition])
def list_effective_parameters(
    type_id: int, session: Session = Depends(get_session)
) -> list[ParameterDefinition]:
    """Return the effective parameter set (own + inherited, decision D3)."""
    return cs.get_effective_parameter_definitions(session, type_id)


@router.post(
    "/{type_id}/parameters",
    response_model=ParameterDefinition,
    status_code=status.HTTP_201_CREATED,
)
def add_parameter_definition(
    type_id: int,
    payload: ParameterDefinitionCreate,
    session: Session = Depends(get_session),
) -> ParameterDefinition:
    return cs.add_parameter_definition(
        session,
        type_id,
        name=payload.name,
        label=payload.label,
        data_type=payload.data_type,
        unit=payload.unit,
        is_filterable=payload.is_filterable,
        is_table_column=payload.is_table_column,
        sort_order=payload.sort_order,
        enum_values=payload.enum_values,
    )

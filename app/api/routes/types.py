"""Component type and parameter-definition endpoints (spec §5-6, §13)."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    ParameterDefinitionCreate,
    ParameterDefinitionRead,
    TypeCreate,
    TypeWithParameters,
)
from app.models.component import ComponentType, ParameterDefinition
from app.services import component_service as cs

router = APIRouter(prefix="/api/types", tags=["types"])


def _read_definitions(
    session: Session, definitions: list[ParameterDefinition]
) -> list[ParameterDefinitionRead]:
    """Enrich definitions with their allowed enum tokens (batched, no N+1)."""
    enums = cs.enum_values_by_definition(
        session, [cast(int, d.id) for d in definitions]
    )
    reads: list[ParameterDefinitionRead] = []
    for d in definitions:
        read = ParameterDefinitionRead.model_validate(d)
        read.enum_values = enums.get(cast(int, d.id), [])
        reads.append(read)
    return reads


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
    own = cs.list_own_parameter_definitions(session, cast(int, ctype.id))
    return TypeWithParameters(
        id=cast(int, ctype.id),
        name=ctype.name,
        parent_id=ctype.parent_id,
        parameters=_read_definitions(session, own),
    )


@router.get("/{type_id}/parameters", response_model=list[ParameterDefinitionRead])
def list_effective_parameters(
    type_id: int, session: Session = Depends(get_session)
) -> list[ParameterDefinitionRead]:
    """Return the effective parameter set (own + inherited, decision D3).

    Each entry carries its allowed enum tokens so a client can render a value
    picker for enum parameters without a follow-up request (spec §13).
    """
    definitions = cs.get_effective_parameter_definitions(session, type_id)
    return _read_definitions(session, definitions)


@router.post(
    "/{type_id}/parameters",
    response_model=ParameterDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
def add_parameter_definition(
    type_id: int,
    payload: ParameterDefinitionCreate,
    session: Session = Depends(get_session),
) -> ParameterDefinitionRead:
    definition = cs.add_parameter_definition(
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
    return _read_definitions(session, [definition])[0]

"""Component type and parameter-definition endpoints (spec §5-6, §13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import ParameterDefinitionCreate, TypeCreate
from app.models.component import ComponentType, ParameterDefinition
from app.services import component_service as cs

router = APIRouter(prefix="/api/types", tags=["types"])


@router.post("", response_model=ComponentType, status_code=status.HTTP_201_CREATED)
def create_type(
    payload: TypeCreate, session: Session = Depends(get_session)
) -> ComponentType:
    return cs.create_type(session, payload.name, parent_id=payload.parent_id)


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

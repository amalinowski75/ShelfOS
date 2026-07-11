"""Component endpoints (spec §4, §12)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import ComponentCreate, ParameterValueSet
from app.auth.deps import current_user_id
from app.models.component import Component, ComponentParameter
from app.services import component_service as cs

router = APIRouter(prefix="/api/components", tags=["components"])


@router.post("", response_model=Component, status_code=status.HTTP_201_CREATED)
def create_component(
    payload: ComponentCreate, session: Session = Depends(get_session)
) -> Component:
    return cs.create_component(
        session,
        payload.type_id,
        manufacturer=payload.manufacturer,
        mpn=payload.mpn,
        package=payload.package,
        mounting_type=payload.mounting_type,
        notes=payload.notes,
    )


@router.get("/{component_id}/parameters", response_model=list[ComponentParameter])
def list_parameter_values(
    component_id: int, session: Session = Depends(get_session)
) -> list[ComponentParameter]:
    return cs.list_parameter_values(session, component_id)


@router.put("/{component_id}/parameters", response_model=ComponentParameter)
def set_parameter_value(
    component_id: int,
    payload: ParameterValueSet,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> ComponentParameter:
    return cs.set_parameter_value(
        session,
        component_id,
        payload.parameter_definition_id,
        payload.value,
        user_id=user_id,
    )

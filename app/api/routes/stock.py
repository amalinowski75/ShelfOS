"""Stock management endpoints (spec §14-15, §17)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import StockAdd, StockCorrection, StockRemove
from app.auth.deps import current_user_id
from app.models.stock import StockMovement
from app.services import stock_service as ss

router = APIRouter(prefix="/api/stock", tags=["stock"])


@router.post("/add", response_model=StockMovement, status_code=status.HTTP_201_CREATED)
def add_stock(
    payload: StockAdd,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> StockMovement:
    return ss.add_stock(
        session,
        component_id=payload.component_id,
        location_id=payload.location_id,
        quantity=payload.quantity,
        user_id=user_id,
        reason=payload.reason,
        container_type=payload.container_type,
        note=payload.note,
    )


@router.post(
    "/remove", response_model=StockMovement, status_code=status.HTTP_201_CREATED
)
def remove_stock(
    payload: StockRemove,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> StockMovement:
    return ss.remove_stock(
        session,
        component_id=payload.component_id,
        location_id=payload.location_id,
        quantity=payload.quantity,
        user_id=user_id,
        reason=payload.reason,
        note=payload.note,
    )


@router.post(
    "/correct", response_model=StockMovement, status_code=status.HTTP_201_CREATED
)
def correct_stock(
    payload: StockCorrection,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> StockMovement:
    return ss.apply_correction(
        session,
        component_id=payload.component_id,
        location_id=payload.location_id,
        delta=payload.delta,
        user_id=user_id,
        note=payload.note,
    )


@router.get("/quantity")
def get_quantity(
    component_id: int,
    location_id: int,
    session: Session = Depends(get_session),
) -> dict[str, int]:
    quantity = ss.get_quantity(session, component_id, location_id)
    return {
        "component_id": component_id,
        "location_id": location_id,
        "quantity": quantity,
    }


@router.get("/total")
def get_total(
    component_id: int, session: Session = Depends(get_session)
) -> dict[str, int]:
    return {
        "component_id": component_id,
        "quantity": ss.total_quantity(session, component_id),
    }

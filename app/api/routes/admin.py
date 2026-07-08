"""Administrative endpoints (spec §20).

Components cannot be deleted from the normal UI; this backend-only endpoint
performs the permanent administrative delete.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.services import component_service as cs

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.delete("/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component(
    component_id: int, session: Session = Depends(get_session)
) -> None:
    cs.hard_delete_component(session, component_id)

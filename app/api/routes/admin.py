"""Administrative endpoints (spec §18, §20).

Admin-only: hard component delete and user account management. The router is
mounted with an admin guard, so every route here requires an admin.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.deps import current_user_id
from app.models.enums import UserRole
from app.services import audit_service
from app.services import component_service as cs
from app.services import user_service as us

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserRead(BaseModel):
    """User representation that never exposes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    role: UserRole
    is_active: bool


class UserCreate(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.USER


class RoleUpdate(BaseModel):
    role: UserRole


class ActiveUpdate(BaseModel):
    is_active: bool


class PasswordUpdate(BaseModel):
    password: str


class AuditEntryRead(BaseModel):
    """One field-level audit-log entry (spec §19)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    field: str
    old_value: str | None
    new_value: str | None
    user_id: int
    timestamp: datetime


@router.delete("/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component(
    component_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    """Permanently delete a component (spec §20)."""
    cs.hard_delete_component(session, component_id, user_id=user_id)


@router.get("/users", response_model=list[UserRead])
def list_users(session: Session = Depends(get_session)) -> list[UserRead]:
    return [UserRead.model_validate(u) for u in us.list_users(session)]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate, session: Session = Depends(get_session)
) -> UserRead:
    user = us.create_user(
        session,
        username=payload.username,
        password=payload.password,
        role=payload.role,
    )
    return UserRead.model_validate(user)


@router.put("/users/{user_id}/role", response_model=UserRead)
def set_role(
    user_id: int, payload: RoleUpdate, session: Session = Depends(get_session)
) -> UserRead:
    return UserRead.model_validate(us.set_role(session, user_id, payload.role))


@router.put("/users/{user_id}/active", response_model=UserRead)
def set_active(
    user_id: int, payload: ActiveUpdate, session: Session = Depends(get_session)
) -> UserRead:
    return UserRead.model_validate(us.set_active(session, user_id, payload.is_active))


@router.put("/users/{user_id}/password", response_model=UserRead)
def set_password(
    user_id: int, payload: PasswordUpdate, session: Session = Depends(get_session)
) -> UserRead:
    return UserRead.model_validate(us.set_password(session, user_id, payload.password))


@router.get("/audit", response_model=list[AuditEntryRead])
def list_audit(
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[AuditEntryRead]:
    """Return audit-log entries, most recent first (spec §19)."""
    entries = audit_service.list_entries(
        session, entity_type=entity_type, entity_id=entity_id, limit=limit
    )
    return [AuditEntryRead.model_validate(e) for e in entries]

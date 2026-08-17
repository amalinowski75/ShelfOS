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
from app.models.component import ComponentType
from app.models.enums import MatchDomain, UserRole
from app.services import audit_service
from app.services import component_service as cs
from app.services import match_rule_service as mrs
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


class TypeUpdate(BaseModel):
    """Rename a component type (its parent and parameters are edited elsewhere)."""

    name: str


class MatchRuleRead(BaseModel):
    """One matching rule, as the admin table shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    domain: MatchDomain
    alias: str
    canonical: str
    parameter_definition_id: int | None
    sort_order: int


class MatchRuleCreate(BaseModel):
    domain: MatchDomain
    alias: str
    canonical: str
    parameter_definition_id: int | None = None
    sort_order: int = 0


class MatchRuleUpdate(BaseModel):
    """Inline edit of a rule; the domain and scope are fixed, so they're absent."""

    alias: str | None = None
    canonical: str | None = None
    sort_order: int | None = None


@router.delete("/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component(
    component_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    """Permanently delete a component (spec §20)."""
    cs.hard_delete_component(session, component_id, user_id=user_id)


@router.patch("/types/{type_id}", response_model=ComponentType)
def rename_type(
    type_id: int,
    payload: TypeUpdate,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> ComponentType:
    """Rename a component type (admin, §13 edit)."""
    return cs.rename_type(session, type_id, name=payload.name, user_id=user_id)


@router.delete("/types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_type(
    type_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    """Delete a component type that nothing depends on (admin, §13 edit)."""
    cs.delete_type(session, type_id, user_id=user_id)


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


@router.get("/match-rules", response_model=list[MatchRuleRead])
def list_match_rules(session: Session = Depends(get_session)) -> list[MatchRuleRead]:
    """Every matching rule, for the engine's editable vocabulary (admin)."""
    return [MatchRuleRead.model_validate(r) for r in mrs.list_rules(session)]


@router.post(
    "/match-rules",
    response_model=MatchRuleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_match_rule(
    payload: MatchRuleCreate, session: Session = Depends(get_session)
) -> MatchRuleRead:
    rule = mrs.create_rule(
        session,
        domain=payload.domain,
        alias=payload.alias,
        canonical=payload.canonical,
        parameter_definition_id=payload.parameter_definition_id,
        sort_order=payload.sort_order,
    )
    return MatchRuleRead.model_validate(rule)


@router.patch("/match-rules/{rule_id}", response_model=MatchRuleRead)
def update_match_rule(
    rule_id: int,
    payload: MatchRuleUpdate,
    session: Session = Depends(get_session),
) -> MatchRuleRead:
    """Edit a rule's alias/target/order in place (only the fields sent change)."""
    changes = payload.model_dump(exclude_unset=True)
    return MatchRuleRead.model_validate(mrs.update_rule(session, rule_id, **changes))


@router.delete("/match-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_match_rule(
    rule_id: int, session: Session = Depends(get_session)
) -> None:
    mrs.delete_rule(session, rule_id)

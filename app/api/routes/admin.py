"""Administrative endpoints (spec §18, §20).

Admin-only: taking a component out of use (and putting it back), type and
parameter administration, the matching rules, and user account management. The
router is mounted with an admin guard, so every route here requires an admin.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import ParameterDefinitionRead
from app.auth.deps import current_user_id
from app.models.component import Component, ComponentType
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


class ParameterDefinitionUpdate(BaseModel):
    """Edit a parameter definition; ``data_type`` is immutable so it is absent.

    A partial PATCH: every field is ``None`` by default and means "leave unchanged",
    so sending one field can't silently reset the others (mirrors ``MatchRuleUpdate``).
    To clear the unit send ``""``, not null. ``enum_values`` is honoured only for enum
    parameters (rejected otherwise).
    """

    name: str | None = None
    label: str | None = None
    unit: str | None = None
    sort_order: int | None = None
    is_table_column: bool | None = None
    is_filterable: bool | None = None
    enum_values: list[str] | None = None


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


class ComponentDelete(BaseModel):
    """Why a component is being taken out of use (§20).

    In the BODY, not the query string: this is free text one person types about
    another person's part, and a query string is the one part of a request that
    is written down everywhere by default — the access log, any proxy in front,
    the browser history, the Referer. The audit log keeps it properly, with the
    actor and the timestamp; a second unmanaged copy in a place with no retention
    policy is not something to hand out for free.
    """

    reason: str | None = PydanticField(default=None, max_length=200)


@router.delete("/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component(
    component_id: int,
    payload: ComponentDelete | None = None,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    """Take a component out of use, keeping its row (spec §20).

    Deliberately not the hard delete it used to be: that left invoice lines and
    stock movements pointing at an id SQLite hands to the next component created,
    so a replacement part inherited the deleted one's purchase history. Nothing
    is lost by keeping the row -- every lookup that could block a replacement
    already ignores deleted components.
    """
    cs.soft_delete_component(
        session,
        component_id,
        user_id=user_id,
        reason=payload.reason if payload else None,
    )


@router.post("/components/{component_id}/restore", response_model=Component)
def restore_component(
    component_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> Component:
    """Put a deleted component back into use (spec §20).

    Refused when a live component has taken over its MPN in the meantime, which
    is precisely what deleting it allowed.
    """
    return cs.restore_component(session, component_id, user_id=user_id)


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


@router.patch("/parameters/{definition_id}", response_model=ParameterDefinitionRead)
def update_parameter_definition(
    definition_id: int,
    payload: ParameterDefinitionUpdate,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> ParameterDefinitionRead:
    """Edit a parameter definition, incl. its enum tokens (admin, §13 edit)."""
    definition = cs.update_parameter_definition(
        session,
        definition_id,
        name=payload.name,
        label=payload.label,
        unit=payload.unit,
        sort_order=payload.sort_order,
        is_table_column=payload.is_table_column,
        is_filterable=payload.is_filterable,
        enum_values=payload.enum_values,
        user_id=user_id,
    )
    read = ParameterDefinitionRead.model_validate(definition)
    read.enum_values = cs.enum_values_of(session, cast(int, definition.id))
    return read


@router.delete("/parameters/{definition_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_parameter_definition(
    definition_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    """Delete a parameter definition no component uses (admin, §13 edit)."""
    cs.delete_parameter_definition(session, definition_id, user_id=user_id)


@router.get("/users", response_model=list[UserRead])
def list_users(session: Session = Depends(get_session)) -> list[UserRead]:
    return [UserRead.model_validate(u) for u in us.list_users(session)]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    actor_id: int = Depends(current_user_id),
) -> UserRead:
    user = us.create_user(
        session,
        username=payload.username,
        password=payload.password,
        role=payload.role,
        actor_id=actor_id,
    )
    return UserRead.model_validate(user)


@router.put("/users/{user_id}/role", response_model=UserRead)
def set_role(
    user_id: int,
    payload: RoleUpdate,
    session: Session = Depends(get_session),
    actor_id: int = Depends(current_user_id),
) -> UserRead:
    return UserRead.model_validate(
        us.set_role(session, user_id, payload.role, actor_id=actor_id)
    )


@router.put("/users/{user_id}/active", response_model=UserRead)
def set_active(
    user_id: int,
    payload: ActiveUpdate,
    session: Session = Depends(get_session),
    actor_id: int = Depends(current_user_id),
) -> UserRead:
    return UserRead.model_validate(
        us.set_active(session, user_id, payload.is_active, actor_id=actor_id)
    )


@router.put("/users/{user_id}/password", response_model=UserRead)
def set_password(
    user_id: int,
    payload: PasswordUpdate,
    session: Session = Depends(get_session),
    actor_id: int = Depends(current_user_id),
) -> UserRead:
    return UserRead.model_validate(
        us.set_password(session, user_id, payload.password, actor_id=actor_id)
    )


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
    payload: MatchRuleCreate,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> MatchRuleRead:
    rule = mrs.create_rule(
        session,
        domain=payload.domain,
        alias=payload.alias,
        canonical=payload.canonical,
        parameter_definition_id=payload.parameter_definition_id,
        sort_order=payload.sort_order,
        user_id=user_id,
    )
    return MatchRuleRead.model_validate(rule)


@router.patch("/match-rules/{rule_id}", response_model=MatchRuleRead)
def update_match_rule(
    rule_id: int,
    payload: MatchRuleUpdate,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> MatchRuleRead:
    """Edit a rule's alias/target/order in place (only the fields sent change)."""
    changes = payload.model_dump(exclude_unset=True)
    return MatchRuleRead.model_validate(
        mrs.update_rule(session, rule_id, **changes, user_id=user_id)
    )


@router.delete("/match-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_match_rule(
    rule_id: int,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> None:
    mrs.delete_rule(session, rule_id, user_id=user_id)

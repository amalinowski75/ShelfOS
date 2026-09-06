"""Authentication endpoints (decision D11).

``POST /api/auth/token`` exchanges credentials for a JWT bearer token used by API
clients; ``GET /api/auth/me`` returns the current account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.deps import (
    bind_session_to_credentials,
    get_current_user,
    require_csrf,
)
from app.auth.throttle import attempt_login
from app.auth.tokens import create_access_token
from app.models.enums import UserRole
from app.models.user import User
from app.services import user_service as us

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: int
    username: str
    role: UserRole


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/token", response_model=TokenResponse)
def login_for_token(
    payload: LoginRequest, request: Request, session: Session = Depends(get_session)
) -> TokenResponse:
    attempt = attempt_login(
        request,
        lambda: us.authenticate(session, payload.username, payload.password),
        username=payload.username,
    )
    if attempt.retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed sign-in attempts; try again later",
            headers={"Retry-After": str(attempt.retry_after)},
        )
    if attempt.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=create_access_token(attempt.user))


@router.get("/me", response_model=MeResponse)
def read_me(user: User = Depends(get_current_user)) -> MeResponse:
    assert user.id is not None
    return MeResponse(id=user.id, username=user.name, role=user.role)


@router.post("/change-password", response_model=MeResponse)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(require_csrf),
) -> MeResponse:
    """Change the caller's own password (any role, incl. read-only).

    Lives on the auth router, which is not behind the read-only write block, so
    a read-only account can still manage its own credentials; CSRF is enforced
    explicitly for cookie-authenticated browser calls.

    Every other sign-in for this account stops working here — that is the point
    of changing a password you think someone else has. The browser making the
    change is carried across (its session is re-bound below) so it is not
    signed out by its own request; an API client authenticated by a bearer
    token is not, and asks for a new one, because a token is exactly the kind
    of credential this is meant to be able to retire.
    """
    updated = us.change_own_password(
        session, user, payload.current_password, payload.new_password
    )
    if getattr(request.state, "auth_via", None) == "session":
        bind_session_to_credentials(request, updated)
    assert updated.id is not None
    return MeResponse(id=updated.id, username=updated.name, role=updated.role)

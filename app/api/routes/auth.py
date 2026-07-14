"""Authentication endpoints (decision D11).

``POST /api/auth/token`` exchanges credentials for a JWT bearer token used by API
clients; ``GET /api/auth/me`` returns the current account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.deps import get_current_user, require_csrf
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
    payload: LoginRequest, session: Session = Depends(get_session)
) -> TokenResponse:
    user = us.authenticate(session, payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=create_access_token(user))


@router.get("/me", response_model=MeResponse)
def read_me(user: User = Depends(get_current_user)) -> MeResponse:
    assert user.id is not None
    return MeResponse(id=user.id, username=user.name, role=user.role)


@router.post("/change-password", response_model=MeResponse)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(require_csrf),
) -> MeResponse:
    """Change the caller's own password (any role, incl. read-only).

    Lives on the auth router, which is not behind the read-only write block, so
    a read-only account can still manage its own credentials; CSRF is enforced
    explicitly for cookie-authenticated browser calls.
    """
    updated = us.change_own_password(
        session, user, payload.current_password, payload.new_password
    )
    assert updated.id is not None
    return MeResponse(id=updated.id, username=updated.name, role=updated.role)

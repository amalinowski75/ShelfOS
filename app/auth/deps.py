"""Authentication/authorization dependencies (decision D11).

Resolves the current user from a JWT bearer token (API) or a session cookie
(web UI), and enforces role-based access:

* reads (GET/HEAD/OPTIONS): any authenticated active user, read-only included;
* writes (other methods): ``user`` or ``admin`` — read-only is rejected;
* admin-only endpoints: ``admin``.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.tokens import decode_token
from app.models.enums import UserRole
from app.models.user import User

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_HEADER = "X-CSRF-Token"
_CSRF_SESSION_KEY = "csrf_token"


def get_optional_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    """Return the current user from a bearer token or session, or ``None``.

    Records how the request authenticated on ``request.state.auth_via``
    (``"bearer"`` or ``"session"``) so CSRF enforcement can target only the
    ambient-cookie path (see :func:`require_csrf`).
    """
    user_id: int | None = None
    auth_via: str | None = None

    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        claims = decode_token(header[7:].strip())
        if claims and claims.get("sub"):
            user_id = int(claims["sub"])
            auth_via = "bearer"

    if user_id is None:
        session_scope = request.scope.get("session")
        if session_scope:
            user_id = session_scope.get("user_id")
            if user_id is not None:
                auth_via = "session"

    request.state.auth_via = auth_via

    if user_id is None:
        return None

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def get_current_user(user: User | None = Depends(get_optional_user)) -> User:
    """Return the authenticated user or raise 401."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_access(request: Request, user: User = Depends(get_current_user)) -> User:
    """Require authentication and block writes for read-only accounts (D11)."""
    if request.method not in _SAFE_METHODS and user.role is UserRole.READ_ONLY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="read-only account cannot modify data",
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require an admin account."""
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin privileges required"
        )
    return user


def require_csrf(request: Request, user: User = Depends(get_current_user)) -> None:
    """Reject unsafe cookie-authenticated requests without a valid CSRF token.

    Bearer-token clients are exempt: they don't rely on the ambient session
    cookie, so they aren't a cross-site request-forgery vector. Browser calls
    authenticated by the session cookie must echo the per-session token (issued
    at login by :func:`issue_csrf_token`) in the ``X-CSRF-Token`` header.
    """
    if request.method in _SAFE_METHODS:
        return
    if getattr(request.state, "auth_via", None) != "session":
        return
    expected = request.session.get(_CSRF_SESSION_KEY)
    provided = request.headers.get(_CSRF_HEADER, "")
    if not expected or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing or invalid CSRF token",
        )


def issue_csrf_token(request: Request) -> str:
    """Store a fresh CSRF token in the session and return it (call at login)."""
    token = secrets.token_urlsafe(32)
    request.session[_CSRF_SESSION_KEY] = token
    return token


def current_user_id(user: User = Depends(get_current_user)) -> int:
    """Return the authenticated user's id (always set after persistence)."""
    assert user.id is not None
    return user.id

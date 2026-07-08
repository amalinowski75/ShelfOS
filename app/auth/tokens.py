"""JWT access tokens for the API (decision D11).

Tokens are stateless HS256 JWTs signed with ``SECRET_KEY``, carrying the user id
and role. Sessions (web UI) are handled separately by Starlette's
SessionMiddleware.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import SECRET_KEY, TOKEN_EXPIRE_HOURS
from app.models.user import User

_ALGORITHM = "HS256"


def create_access_token(user: User) -> str:
    """Create a signed JWT for the given user."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "role": user.role.value,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT, returning its claims or ``None`` if invalid."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None

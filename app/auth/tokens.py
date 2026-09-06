"""JWT access tokens for the API, and the credential binding both sign-in
mechanisms share (decision D11).

Tokens are stateless HS256 JWTs signed with ``SECRET_KEY``, carrying the user id
and role. Sessions (web UI) are handled separately by Starlette's
SessionMiddleware.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import SECRET_KEY, TOKEN_EXPIRE_HOURS
from app.models.user import User

_ALGORITHM = "HS256"

# JWT claim, and session key, carrying the fingerprint below.
CREDENTIAL_CLAIM = "cf"


def credential_fingerprint(user: User) -> str | None:
    """A stand-in for the user's *current* password, carried by a sign-in.

    Stateless tokens and signed session cookies both outlive the password they
    were issued against: nothing about them changes when it does, so a leaked
    one keeps working for its full lifetime and changing the password is no way
    to end it. Binding each to a value derived from the password hash fixes
    that without any server-side session store to keep — setting a new password
    changes the hash, which changes this, which retires every sign-in issued
    against the old one.

    It is an HMAC keyed by ``SECRET_KEY``, not the hash itself: the value
    travels in a JWT payload, which is signed but readable, and the bcrypt hash
    is not ours to hand out. (This is the mechanism Django calls the session
    auth hash.)

    ``None`` for an account with no password — the seeded system user — which
    cannot sign in, and so must never satisfy this check either.
    """
    if user.password_hash is None:
        return None
    return hmac.new(
        SECRET_KEY.encode(), user.password_hash.encode(), hashlib.sha256
    ).hexdigest()


def create_access_token(user: User) -> str:
    """Create a signed JWT for the given user."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "role": user.role.value,
        CREDENTIAL_CLAIM: credential_fingerprint(user),
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

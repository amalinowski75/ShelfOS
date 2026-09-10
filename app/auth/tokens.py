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

# A token that may do ONE thing rather than everything its owner may do. The
# claim is what keeps the two apart: a full sign-in never carries it, and
# ``get_optional_user`` refuses any token that does — so a narrow token handed
# to a script can never be presented as its owner anywhere else.
SCOPE_CLAIM = "scope"
ENROLL_SCOPE = "label-enroll"


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

    ``None`` for an account with no password — the one demo data is attributed
    to is the only such account ShelfOS makes — which
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


def create_enroll_token(user: User, *, hours: int) -> str:
    """A token good for registering a label printer's key, and nothing else.

    It travels inside a downloaded shell script, so it is deliberately not a
    sign-in: it names a scope, and the only endpoint that accepts it checks
    that scope before it does anything. Short-lived, and bound to the password
    it was issued against like every other sign-in here — changing the password
    retires it with the rest.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        SCOPE_CLAIM: ENROLL_SCOPE,
        CREDENTIAL_CLAIM: credential_fingerprint(user),
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=_ALGORITHM)


def decode_scoped_token(token: str, scope: str) -> dict[str, Any] | None:
    """Claims of a valid token carrying exactly ``scope``, or ``None``.

    Signature, expiry and scope in one place, so no caller can be tempted to
    check two of the three.
    """
    claims = decode_token(token)
    if claims is None or claims.get(SCOPE_CLAIM) != scope:
        return None
    return claims


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT, returning its claims or ``None`` if invalid."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None

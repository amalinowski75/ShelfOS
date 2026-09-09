"""Registering a label printer's machine, from the machine itself (spec §7).

Its own router, mounted without the usual guard, because the caller is not a
person at a browser: it is the setup script running on somebody's laptop,
carrying a token that may do this one thing. The guard is here instead, and it
is narrower than the usual one — a token with a scope is refused everywhere else
in the app (see ``get_optional_user``), and this endpoint refuses anything that
is not that scope.

Why this exists at all: the person with the printer has a ShelfOS login and
nothing more. Requiring them to add a key on the server meant having an account
there, knowing which one, and knowing how — which for most people means it does
not happen. They are already authenticated to ShelfOS; ShelfOS can take the key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.tokens import (
    CREDENTIAL_CLAIM,
    ENROLL_SCOPE,
    credential_fingerprint,
    decode_scoped_token,
)
from app.models.enums import UserRole
from app.models.user import User
from app.services import audit_service, tunnel_keys
from app.services import label_setup as setup

router = APIRouter(prefix="/api/labels/setup", tags=["labels"])


class EnrollRequest(BaseModel):
    """One public key, as ``ssh-keygen`` wrote it."""

    public_key: str = Field(max_length=4096)


class EnrollResult(BaseModel):
    """What was registered, in the words the script prints back."""

    comment: str
    fingerprint: str
    port: int
    registered: int


def _caller(request: Request, session: Session) -> User:
    """The account whose download this script came from, or 401/403.

    Every check a sign-in would do, plus the scope: the token must be signed,
    unexpired, carry this scope and no other, name an active account, and match
    the password it was issued against — so a printer registered with a token
    minted before a password change stops being registerable with it.
    """
    header = request.headers.get("Authorization", "")
    claims = (
        decode_scoped_token(header[7:].strip(), ENROLL_SCOPE)
        if header.lower().startswith("bearer ")
        else None
    )
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "this script's registration token is missing or has expired — "
                "download the script again from the label-printer page"
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user = session.get(User, int(claims.get("sub", "")))
    except (TypeError, ValueError):
        user = None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="the account this script was downloaded by can no longer sign in",
            headers={"WWW-Authenticate": "Bearer"},
        )
    expected = credential_fingerprint(user)
    presented = claims.get(CREDENTIAL_CLAIM)
    if expected is None or presented != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "the password has changed since this script was downloaded — "
                "download it again from the label-printer page"
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Registering a machine changes what this server accepts, which is a write,
    # and a read-only account does not make those. It may still read the page
    # and download the script; the last step needs somebody who may change
    # something here.
    if user.role is UserRole.READ_ONLY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="read-only account cannot register a printer",
        )
    return user


@router.post("/enroll", response_model=EnrollResult)
def enroll_printer(
    payload: EnrollRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> EnrollResult:
    """Authorise the calling machine's key for the tunnel account (§7).

    No CSRF token: this is not a browser and carries no cookie, so there is no
    ambient authority to forge — the bearer token is the whole authorisation,
    exactly as for the API's own tokens.
    """
    user = _caller(request, session)
    assert user.id is not None  # a persisted account, or _caller would have raised
    port = setup.default_bridge_port()
    key = tunnel_keys.enroll(payload.public_key, port=port)
    # Who let which machine in, kept where every other consequential change is.
    audit_service.record_change(
        session,
        entity_type="label_printer",
        entity_id=0,
        field="tunnel_key",
        old_value=None,
        new_value=f"{key.comment} {key.fingerprint}",
        user_id=user.id,
    )
    session.commit()
    return EnrollResult(
        comment=key.comment,
        fingerprint=key.fingerprint,
        port=port,
        registered=len(tunnel_keys.list_keys()),
    )

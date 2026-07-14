"""User account business logic (spec §18, decision D11).

Handles password hashing (bcrypt), authentication, and admin-driven account
management. There is no self-registration: accounts are created by an admin.
"""

from __future__ import annotations

from typing import cast

import bcrypt
from sqlmodel import Session, col, select

from app.models.enums import UserRole
from app.models.user import User
from app.services._common import require_entity
from app.services.errors import NotFoundError, ValidationError

# bcrypt hashes only the first 72 bytes of a password and ignores the rest, so
# two long passwords differing only past that point would collide. Reject them
# up front instead of silently truncating.
_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """Return a bcrypt hash for a plaintext password."""
    if len(password.encode()) > _MAX_PASSWORD_BYTES:
        raise ValidationError(
            f"password must be at most {_MAX_PASSWORD_BYTES} bytes"
        )
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def get_by_username(session: Session, username: str) -> User | None:
    """Return the user with the given username, or ``None``."""
    return session.exec(select(User).where(User.name == username)).first()


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    role: UserRole = UserRole.USER,
    is_active: bool = True,
) -> User:
    """Create a user with a hashed password (admin action, D11)."""
    if not username.strip():
        raise ValidationError("username must not be empty")
    if not password:
        raise ValidationError("password must not be empty")
    if get_by_username(session, username) is not None:
        raise ValidationError(f"username {username!r} is already taken")

    user = User(
        name=username,
        role=role,
        is_active=is_active,
        password_hash=hash_password(password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    """Return the user if credentials are valid and the account is active."""
    user = get_by_username(session, username)
    if user is None or not user.is_active or user.password_hash is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def list_users(session: Session) -> list[User]:
    """Return all users ordered by username."""
    return list(session.exec(select(User).order_by(col(User.name))).all())


def set_role(session: Session, user_id: int, role: UserRole) -> User:
    """Change a user's role."""
    user = require_entity(session, User, user_id, "user")
    if role is not UserRole.ADMIN and _is_last_login_admin(session, user):
        raise ValidationError("cannot demote the last active admin")
    user.role = role
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def set_active(session: Session, user_id: int, is_active: bool) -> User:
    """Enable or disable a user account."""
    user = require_entity(session, User, user_id, "user")
    if not is_active and _is_last_login_admin(session, user):
        raise ValidationError("cannot disable the last active admin")
    user.is_active = is_active
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _is_last_login_admin(session: Session, user: User) -> bool:
    """True when ``user`` is the only remaining admin able to log in.

    Guards against an admin locking every human admin out of the system by
    demoting or disabling the last one (the passwordless system user does not
    count, as it cannot log in).
    """
    if (
        user.role is not UserRole.ADMIN
        or not user.is_active
        or user.password_hash is None
    ):
        return False
    login_admins = session.exec(
        select(User).where(
            User.role == UserRole.ADMIN,
            col(User.is_active).is_(True),
            col(User.password_hash).is_not(None),
        )
    ).all()
    return len(login_admins) <= 1


def set_password(session: Session, user_id: int, password: str) -> User:
    """Set a new password for a user."""
    if not password:
        raise ValidationError("password must not be empty")
    user = require_entity(session, User, user_id, "user")
    user.password_hash = hash_password(password)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def change_own_password(
    session: Session, user: User, current_password: str, new_password: str
) -> User:
    """Let a signed-in user change their own password (any role, self-service).

    The current password must be verified first: it is standard practice and
    stops a bystander at an unlocked browser from setting a new password. It does
    not by itself revoke other active sessions or already-issued bearer tokens
    (those are stateless, D11), so it is not a full account-takeover recovery.
    Reuses ``set_password`` so the hashing and length checks stay in one place.
    """
    if user.password_hash is None or not verify_password(
        current_password, user.password_hash
    ):
        raise ValidationError("current password is incorrect")
    return set_password(session, cast(int, user.id), new_password)


def ensure_admin(session: Session, *, username: str, password: str) -> User:
    """Seed a bootstrap admin if no login-capable admin exists yet (D11).

    The seeded "system" user is an admin but cannot log in (no password), so it
    is explicitly ignored here. Returns the existing or newly created admin;
    idempotent.
    """
    existing_admin = session.exec(
        select(User).where(
            User.role == UserRole.ADMIN,
            col(User.password_hash).is_not(None),
        )
    ).first()
    if existing_admin is not None:
        return existing_admin
    if get_by_username(session, username) is not None:
        raise NotFoundError(  # pragma: no cover - defensive
            f"cannot seed admin: username {username!r} already exists"
        )
    return create_user(
        session,
        username=username,
        password=password,
        role=UserRole.ADMIN,
    )

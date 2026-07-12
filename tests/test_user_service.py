"""Tests for user_service: hashing, authentication, account management."""

from __future__ import annotations

import pytest
from app.models.enums import UserRole
from app.seed import ensure_system_user
from app.services import user_service as us
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session


def test_password_hash_roundtrip() -> None:
    hashed = us.hash_password("s3cret")
    assert hashed != "s3cret"
    assert us.verify_password("s3cret", hashed)
    assert not us.verify_password("wrong", hashed)


def test_create_user_and_authenticate(session: Session) -> None:
    us.create_user(session, username="alice", password="pw", role=UserRole.USER)
    user = us.authenticate(session, "alice", "pw")
    assert user is not None
    assert user.role is UserRole.USER
    assert user.password_hash is not None


def test_authenticate_rejects_bad_password(session: Session) -> None:
    us.create_user(session, username="bob", password="pw")
    assert us.authenticate(session, "bob", "nope") is None
    assert us.authenticate(session, "ghost", "pw") is None


def test_authenticate_rejects_inactive_user(session: Session) -> None:
    user = us.create_user(session, username="carol", password="pw")
    us.set_active(session, user.id, False)
    assert us.authenticate(session, "carol", "pw") is None


def test_system_user_cannot_log_in(session: Session) -> None:
    ensure_system_user(session)
    # No password set, so authentication must fail.
    assert us.authenticate(session, "system", "") is None


def test_duplicate_username_rejected(session: Session) -> None:
    us.create_user(session, username="dave", password="pw")
    with pytest.raises(ValidationError):
        us.create_user(session, username="dave", password="other")


def test_empty_username_or_password_rejected(session: Session) -> None:
    with pytest.raises(ValidationError):
        us.create_user(session, username="  ", password="pw")
    with pytest.raises(ValidationError):
        us.create_user(session, username="eve", password="")


def test_set_role_and_password(session: Session) -> None:
    user = us.create_user(session, username="frank", password="pw")
    us.set_role(session, user.id, UserRole.ADMIN)
    us.set_password(session, user.id, "newpw")

    refreshed = us.authenticate(session, "frank", "newpw")
    assert refreshed is not None
    assert refreshed.role is UserRole.ADMIN
    assert us.authenticate(session, "frank", "pw") is None


def test_set_role_unknown_user_raises(session: Session) -> None:
    with pytest.raises(NotFoundError):
        us.set_role(session, 999, UserRole.ADMIN)


def test_password_over_72_bytes_rejected(session: Session) -> None:
    """bcrypt ignores bytes past 72; reject instead of silently truncating (L2)."""
    with pytest.raises(ValidationError):
        us.create_user(session, username="mallory", password="a" * 73)
    # Exactly 72 bytes is fine and round-trips.
    user = us.create_user(session, username="trent", password="a" * 72)
    assert us.authenticate(session, "trent", "a" * 72) is not None
    with pytest.raises(ValidationError):
        us.set_password(session, user.id, "a" * 73)


def test_cannot_lock_out_last_admin(session: Session) -> None:
    """The last login-capable admin can be neither demoted nor disabled (L3)."""
    admin = us.create_user(
        session, username="admin", password="pw", role=UserRole.ADMIN
    )
    # A passwordless system admin does not count as login-capable.
    ensure_system_user(session)

    with pytest.raises(ValidationError):
        us.set_role(session, admin.id, UserRole.USER)
    with pytest.raises(ValidationError):
        us.set_active(session, admin.id, False)

    # A second real admin lifts the restriction on the first.
    other = us.create_user(
        session, username="admin2", password="pw", role=UserRole.ADMIN
    )
    us.set_active(session, admin.id, False)
    assert us.authenticate(session, "admin", "pw") is None
    # Now `other` is the last one and is protected in turn.
    with pytest.raises(ValidationError):
        us.set_role(session, other.id, UserRole.USER)


def test_ensure_admin_is_idempotent(session: Session) -> None:
    first = us.ensure_admin(session, username="admin", password="admin")
    second = us.ensure_admin(session, username="admin", password="admin")
    assert first.id == second.id
    assert first.role is UserRole.ADMIN

    # A second call must not create another admin even with a new username.
    third = us.ensure_admin(session, username="root", password="x")
    assert third.id == first.id


def test_ensure_admin_ignores_non_login_system_user(session: Session) -> None:
    # The system user is an admin but cannot log in; a real admin must still be
    # seeded so someone can actually authenticate.
    system = ensure_system_user(session)
    admin = us.ensure_admin(session, username="admin", password="admin")
    assert admin.id != system.id
    assert admin.password_hash is not None
    assert us.authenticate(session, "admin", "admin") is not None


def test_list_users_ordered(session: Session) -> None:
    us.create_user(session, username="zoe", password="pw")
    us.create_user(session, username="amy", password="pw")
    assert [u.name for u in us.list_users(session)] == ["amy", "zoe"]


def test_verify_password_tolerates_malformed_hash() -> None:
    """A stored value that isn't a bcrypt hash makes ``checkpw`` raise; the
    helper must report "not verified" rather than propagate the ValueError."""
    assert us.verify_password("pw", "not-a-bcrypt-hash") is False


def test_set_password_rejects_empty(session: Session) -> None:
    user = us.create_user(session, username="carol", password="pw")
    with pytest.raises(ValidationError):
        us.set_password(session, user.id, "")

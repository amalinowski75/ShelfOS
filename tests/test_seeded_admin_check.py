"""Startup must judge the admin account, not the environment variable.

``SHELFOS_ADMIN_PASSWORD`` seeds an admin only when there is no login-capable
one yet, so on any database past its first run it says nothing about the
account that actually exists. Checking the variable therefore passed an
instance that was still open on ``admin``/``admin``.
"""

from __future__ import annotations

import pytest
from app import config
from app import main as app_main
from app.models.enums import UserRole
from app.services import user_service as us
from sqlmodel import Session


def _seed_default_admin(session: Session) -> None:
    us.create_user(
        session,
        username="admin",
        password=config.DEFAULT_ADMIN_PASSWORD,
        role=UserRole.ADMIN,
        enforce_policy=False,
    )


def test_production_refuses_an_admin_still_on_the_default_password(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_default_admin(session)
    monkeypatch.setattr(config, "ENV", "production")
    with pytest.raises(RuntimeError, match="still have the default password"):
        app_main._check_seeded_admin_password(session)


def test_the_refusal_names_the_account_and_the_way_out(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_default_admin(session)
    monkeypatch.setattr(config, "ENV", "production")
    with pytest.raises(RuntimeError) as caught:
        app_main._check_seeded_admin_password(session)
    message = str(caught.value)
    assert "admin" in message
    assert "scripts/set_password.py" in message
    # And it says why setting the variable did not help, which is the whole trap.
    assert "SHELFOS_ADMIN_PASSWORD does not change an account" in message


def test_a_configured_admin_password_does_not_excuse_the_account(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: the variable was set, so every earlier check passed."""
    _seed_default_admin(session)
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "a-real-admin-password")
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-production-secret-value-32b")
    assert not config.is_using_default_admin_password()
    app_main._check_insecure_defaults()  # the old check is happy
    with pytest.raises(RuntimeError):  # the new one is not
        app_main._check_seeded_admin_password(session)


def test_development_only_warns(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    _seed_default_admin(session)
    monkeypatch.setattr(config, "ENV", "development")
    with caplog.at_level(logging.WARNING, logger="shelfos"):
        app_main._check_seeded_admin_password(session)  # does not raise
    assert any("default password" in r.getMessage() for r in caplog.records)


def test_a_changed_password_passes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_default_admin(session)
    admin = us.get_by_username(session, "admin")
    assert admin is not None
    us.set_password(session, admin.id, "a-real-admin-password", actor_id=admin.id)
    monkeypatch.setattr(config, "ENV", "production")
    app_main._check_seeded_admin_password(session)  # does not raise


def test_an_empty_database_passes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ENV", "production")
    app_main._check_seeded_admin_password(session)


def test_a_disabled_admin_is_not_a_way_in(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot sign in, so it is not an exposure — and not a reason to refuse."""
    _seed_default_admin(session)
    admin = us.get_by_username(session, "admin")
    assert admin is not None
    # Another admin, so disabling this one is allowed.
    us.create_user(
        session, username="other", password="other-password", role=UserRole.ADMIN
    )
    us.set_active(session, admin.id, False, actor_id=admin.id)
    monkeypatch.setattr(config, "ENV", "production")
    app_main._check_seeded_admin_password(session)


def test_a_non_admin_on_the_default_password_is_not_checked(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy stops anyone setting one now; only a seeded admin can have it."""
    us.create_user(
        session,
        username="worker",
        password=config.DEFAULT_ADMIN_PASSWORD,
        role=UserRole.USER,
        enforce_policy=False,
    )
    monkeypatch.setattr(config, "ENV", "production")
    app_main._check_seeded_admin_password(session)


def test_admins_with_password_finds_only_matching_admins(session: Session) -> None:
    _seed_default_admin(session)
    us.create_user(
        session, username="other", password="other-password", role=UserRole.ADMIN
    )
    found = us.admins_with_password(session, config.DEFAULT_ADMIN_PASSWORD)
    assert [u.name for u in found] == ["admin"]
    assert us.admins_with_password(session, "nothing-uses-this") == []

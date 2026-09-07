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


# --- The variable is judged only where it is about to be used ----------------


def test_an_inert_admin_password_variable_does_not_block_the_boot(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror-image false positive of the bug this branch fixed.

    An operator who reads the README, fixes the real account, and then drops the
    variable it says is pointless must not be told to set it again — with the
    account check standing right there, satisfied.
    """
    us.create_user(
        session,
        username="admin",
        password="a-real-admin-password",
        role=UserRole.ADMIN,
    )
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", config.DEFAULT_ADMIN_PASSWORD)
    app_main._check_admin_password_source(session)  # does not raise
    app_main._check_seeded_admin_password(session)  # nor this


def test_the_variable_still_guards_a_first_run(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no admin yet it is about to become one's password, so it is judged."""
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", config.DEFAULT_ADMIN_PASSWORD)
    with pytest.raises(RuntimeError, match="no admin yet"):
        app_main._check_admin_password_source(session)


def test_a_seeded_but_unusable_admin_does_not_count_as_one(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The system user is an admin that cannot sign in, so a seed is still due."""
    from app.seed import ensure_demo_user

    ensure_demo_user(session)
    assert not us.has_login_capable_admin(session)
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", config.DEFAULT_ADMIN_PASSWORD)
    with pytest.raises(RuntimeError):
        app_main._check_admin_password_source(session)


# --- Re-enabling an account that still has the default password --------------


def test_enabling_an_account_on_the_default_password_is_refused(
    session: Session,
) -> None:
    """Startup checks active admins, so a disabled one holding it is invisible;
    turning it back on would open the instance now and break the next boot."""
    from app.services.errors import ValidationError

    _seed_default_admin(session)
    exposed = us.get_by_username(session, "admin")
    assert exposed is not None
    other = us.create_user(
        session, username="other", password="other-password", role=UserRole.ADMIN
    )
    us.set_active(session, exposed.id, False, actor_id=other.id)

    with pytest.raises(ValidationError, match="default password"):
        us.set_active(session, exposed.id, True, actor_id=other.id)

    # Setting a real password first is the way through.
    us.set_password(session, exposed.id, "a-real-admin-password", actor_id=other.id)
    assert us.set_active(session, exposed.id, True, actor_id=other.id).is_active


def test_disabling_such_an_account_is_still_allowed(session: Session) -> None:
    """The refusal is about letting it back in, not about tidying it away."""
    _seed_default_admin(session)
    exposed = us.get_by_username(session, "admin")
    assert exposed is not None
    other = us.create_user(
        session, username="other", password="other-password", role=UserRole.ADMIN
    )
    assert not us.set_active(session, exposed.id, False, actor_id=other.id).is_active


def test_re_enabling_an_ordinary_account_is_checked_too(session: Session) -> None:
    """One account, one bcrypt: no reason to narrow this to admins as startup is."""
    from app.services.errors import ValidationError

    worker = us.create_user(
        session,
        username="worker",
        password=config.DEFAULT_ADMIN_PASSWORD,
        role=UserRole.USER,
        enforce_policy=False,
    )
    admin = us.create_user(
        session,
        username="admin",
        password="a-real-admin-password",
        role=UserRole.ADMIN,
    )
    us.set_active(session, worker.id, False, actor_id=admin.id)
    with pytest.raises(ValidationError, match="default password"):
        us.set_active(session, worker.id, True, actor_id=admin.id)


def test_the_production_message_leads_with_the_only_possible_remedy(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app is refusing to start, so "sign in and change it" has nowhere to go."""
    _seed_default_admin(session)
    monkeypatch.setattr(config, "ENV", "production")
    with pytest.raises(RuntimeError) as caught:
        app_main._check_seeded_admin_password(session)
    message = str(caught.value)
    assert "Sign in" not in message
    assert message.index("set_password.py") < len(message)


def test_the_development_message_offers_signing_in(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Here the app does start, so the ordinary way round is the ordinary advice."""
    import logging

    _seed_default_admin(session)
    monkeypatch.setattr(config, "ENV", "development")
    with caplog.at_level(logging.WARNING, logger="shelfos"):
        app_main._check_seeded_admin_password(session)
    message = next(
        r.getMessage() for r in caplog.records if "default password" in r.getMessage()
    )
    assert message.index("Sign in") < message.index("set_password.py")


# --- the account demo data is attributed to ----------------------------------


def test_startup_creates_no_demo_account(session: Session) -> None:
    """It is part of the demo data, not of every installation.

    Seeding it at startup gave a fresh production install a second admin-role
    row that nothing would ever use, sitting in the users table looking like an
    account somebody had forgotten about.
    """
    from app.seed import DEMO_USER_NAME

    app_main._check_admin_password_source(session)
    us.ensure_admin(session, username="admin", password="a-real-admin-password")
    app_main._check_seeded_admin_password(session)
    assert [u.name for u in us.list_users(session)] == ["admin"]
    assert us.get_by_username(session, DEMO_USER_NAME) is None


def test_the_demo_account_is_created_with_the_demo_data(session: Session) -> None:
    from app.seed import DEMO_USER_NAME, ensure_demo_user

    user = ensure_demo_user(session)
    assert user.name == DEMO_USER_NAME
    assert user.password_hash is None  # so it cannot sign in
    assert ensure_demo_user(session).id == user.id  # idempotent


def test_an_older_databases_system_account_is_adopted_not_renamed(
    session: Session,
) -> None:
    """Its name is what the audit log shows against everything it ever did.

    "system did this" was true when it was written; renaming it now would make
    old entries claim something that was never on screen. So the row is reused
    as it stands, and only a new database gets the better name.
    """
    from app.models.enums import UserRole
    from app.models.user import User
    from app.seed import ensure_demo_user

    legacy = User(name="system", role=UserRole.ADMIN)
    session.add(legacy)
    session.commit()
    session.refresh(legacy)

    adopted = ensure_demo_user(session)
    assert adopted.id == legacy.id
    assert adopted.name == "system"
    assert len(us.list_users(session)) == 1


def test_an_account_that_cannot_sign_in_cannot_be_given_a_password(
    session: Session,
) -> None:
    """The command-line tool always refused this; the users page did not."""
    from app.seed import ensure_demo_user
    from app.services.errors import ValidationError

    demo = ensure_demo_user(session)
    admin = us.create_user(
        session,
        username="admin",
        password="a-real-admin-password",
        role=UserRole.ADMIN,
    )
    with pytest.raises(ValidationError, match="not a sign-in account"):
        us.set_password(session, demo.id, "a-real-password", actor_id=admin.id)
    assert us.get_by_username(session, "demo") is not None
    assert us.get_by_username(session, "demo").password_hash is None

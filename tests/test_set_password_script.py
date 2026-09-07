"""The command-line password reset — the way out of the startup refusal.

Without it, an instance that refuses to start because an admin is still on the
default password could not be fixed: changing it through the UI needs the app
to be running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from app.models.enums import UserRole
from app.services import user_service as us
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set_password.py"


@pytest.fixture
def file_db(tmp_path: Path) -> tuple[str, Engine]:
    """A real file-backed database: the script runs in its own process."""
    path = tmp_path / "shelfos.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    import app.models  # noqa: F401
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        us.create_user(
            session,
            username="admin",
            password="admin",
            role=UserRole.ADMIN,
            enforce_policy=False,
        )
    return url, engine


def _run(url: str, *args: str, new_password: str | None = None):  # type: ignore[no-untyped-def]
    env = {"DATABASE_URL": url, "PATH": "/usr/bin:/bin"}
    if new_password is not None:
        env["SHELFOS_NEW_PASSWORD"] = new_password
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        # Explicit, not inherited: under `pytest -s` the parent's stdin is a real
        # terminal, and the no-terminal test would then hand the script one, so
        # it would prompt with getpass and block the whole run forever.
        stdin=subprocess.DEVNULL,
        env={**env, "PYTHONPATH": str(_SCRIPT.parents[1])},
    )


def test_it_sets_the_password(file_db: tuple[str, Engine]) -> None:
    url, engine = file_db
    result = _run(url, "admin", new_password="a-real-admin-password")
    assert result.returncode == 0, result.stderr
    with Session(engine) as session:
        assert us.authenticate(session, "admin", "a-real-admin-password") is not None
        assert us.authenticate(session, "admin", "admin") is None


def test_it_enforces_the_password_policy(file_db: tuple[str, Engine]) -> None:
    """Or it would be a way to put an account back below the floor."""
    url, engine = file_db
    result = _run(url, "admin", new_password="short")
    assert result.returncode != 0
    assert "at least" in result.stdout + result.stderr
    with Session(engine) as session:
        assert us.authenticate(session, "admin", "admin") is not None


def test_it_refuses_an_unknown_account(file_db: tuple[str, Engine]) -> None:
    url, _ = file_db
    result = _run(url, "nobody", new_password="a-real-admin-password")
    assert result.returncode != 0
    assert "No account named" in result.stdout + result.stderr


def test_it_refuses_an_account_that_cannot_sign_in(file_db: tuple[str, Engine]) -> None:
    """Giving the demo-data actor a password would only create a way in."""
    url, engine = file_db
    from app.seed import DEMO_USER_NAME, ensure_demo_user

    with Session(engine) as session:
        ensure_demo_user(session)
    result = _run(url, DEMO_USER_NAME, new_password="a-real-admin-password")
    assert result.returncode != 0
    assert "cannot sign in" in result.stdout + result.stderr


def test_it_lists_the_accounts(file_db: tuple[str, Engine]) -> None:
    url, _ = file_db
    result = _run(url, "--list")
    assert result.returncode == 0, result.stderr
    assert "admin" in result.stdout


def test_it_refuses_to_prompt_with_no_terminal(file_db: tuple[str, Engine]) -> None:
    """Unattended and unset must fail loudly, not hang or read from a pipe."""
    url, _ = file_db
    result = _run(url, "admin")
    assert result.returncode != 0
    assert "SHELFOS_NEW_PASSWORD" in result.stdout + result.stderr

"""Shared pytest fixtures.

Every test runs against an isolated in-memory SQLite database so tests are fast
and never touch real data. A ``StaticPool`` keeps the single in-memory
connection alive for the duration of a test.

The same goes for configuration. ``app.config`` reads ``SHELFOS_*`` from the
environment when it is imported, so a suite run in a shell that has the real
deployment's settings loaded is not testing the defaults it asserts — and, worse
than a confusing failure, ``SHELFOS_LABEL_DEVICE`` pointed at a Brother QL means
the one test that deliberately prints *without* a device override feeds a label
out of somebody's actual printer. So the environment is emptied of ``SHELFOS_*``
here, before the first import of anything under ``app``, and every test then sees
the documented defaults exactly as CI does. A test that wants a setting sets it
itself, with ``monkeypatch``.

``DATABASE_URL`` goes with them although it carries no prefix: a deploy env file
sets it beside the others, ``app.db`` builds its module-level engine from it when
it is imported — three lines below — and that engine is the one the API depends
on. Left in place it either breaks collection outright (a Postgres URL, with no
driver installed) or, worse, quietly points the suite at the real database.
"""

from __future__ import annotations

import os

# Anything here is read when `app` is imported, so it must go before that happens.
_AMBIENT = [
    _name
    for _name in os.environ
    if _name.startswith("SHELFOS_") or _name == "DATABASE_URL"
]
for _leaked in _AMBIENT:
    del os.environ[_leaked]

from collections.abc import Iterator  # noqa: E402

import app.models  # noqa: E402, F401  (register tables on SQLModel.metadata)
import pytest  # noqa: E402
from app import config  # noqa: E402
from app.api.deps import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_printer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from a physical label printer.

    The scrub above already clears both of these; this is the second lock on the
    same door, because the cost of it being wrong is not a red test but wasted
    tape in a machine nobody is standing next to. A test that means to print sets
    the device itself (a temporary file, or the pty in ``tests.fake_printer``),
    which overrides this.

    Both settings have to be pinned, because there are two ways to a printer:
    ``configured_device()`` answers ``LABEL_DEVICE`` when it is set, and otherwise
    falls through to the tunnel on loopback as soon as any key is registered —
    which ``TUNNEL_KEYS_FILE`` decides. Emptying only the first would not close
    the door, it would *choose* the second, and on a machine with a live bridge
    that is once again a real QL.
    """
    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    monkeypatch.setattr(config, "TUNNEL_KEYS_FILE", "")


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Provide a fresh in-memory database engine with all tables created."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    try:
        yield test_engine
    finally:
        test_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Provide a session bound to the in-memory engine."""
    with Session(engine) as db_session:
        yield db_session


def _build_app(engine: Engine):  # type: ignore[no-untyped-def]
    """Build an app bound to the given in-memory engine."""
    app = create_app(create_tables=False)

    def override_get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


def web_login(client, username: str, password: str):  # type: ignore[no-untyped-def]
    """Sign in through the HTML form, as a browser does.

    The form carries a CSRF token minted by GET /login, so a POST that skipped
    the page is refused — which is the point of the check, and the reason every
    test that signs in has to fetch the page first.
    """
    import re

    page = client.get("/login")
    match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    assert match and match.group(1), "login page did not expose a CSRF token"
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": match.group(1),
        },
        follow_redirects=False,
    )


def web_logout(client):  # type: ignore[no-untyped-def]
    """Sign out through the form, token and all (see :func:`web_login`)."""
    import re

    page = client.get("/")
    match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    assert match and match.group(1), "page did not expose a CSRF token"
    return client.post(
        "/logout", data={"csrf_token": match.group(1)}, follow_redirects=False
    )


@pytest.fixture
def anon_client(engine: Engine) -> Iterator[object]:
    """An unauthenticated TestClient bound to the in-memory engine."""
    from fastapi.testclient import TestClient

    with TestClient(_build_app(engine)) as test_client:
        yield test_client


@pytest.fixture
def client(engine: Engine) -> Iterator[object]:
    """An admin-authenticated TestClient (default for most tests)."""
    from app.models.enums import UserRole
    from app.services import match_rule_service as mrs
    from app.services import user_service as us
    from fastapi.testclient import TestClient

    app = _build_app(engine)
    with Session(engine) as setup_session:
        us.create_user(
            setup_session,
            username="admin",
            password="admin-password",
            role=UserRole.ADMIN,
        )
        # Mirror the app's startup seed so the matching engine has its defaults.
        mrs.seed_default_rules(setup_session)

    with TestClient(app) as test_client:
        token = test_client.post(
            "/api/auth/token", json={"username": "admin", "password": "admin-password"}
        ).json()["access_token"]
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client

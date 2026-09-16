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

Speed is a fixture too. Every password the suite hashes is a constant it wrote
itself, so ``cheap_password_hashing`` turns bcrypt's cost factor down to its
minimum for the run — the same code, the same stored format, the same
verification, just without the deliberate slowness that only matters to somebody
attacking a stolen database. It was the majority of the suite's wall clock.

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
from app.services import user_service  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

# Read before anything lowers it, so a test that wants the real thing back
# has a number to restore rather than a second copy of the constant.
_PRODUCTION_BCRYPT_ROUNDS = user_service.BCRYPT_ROUNDS


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


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash the suite's throwaway passwords at a cost factor that buys nothing.

    bcrypt's shipped cost factor is deliberately expensive — about a fifth of a
    second a hash — because a stolen database should be. None of that applies
    here: every password the suite hashes is a constant written three lines
    above the assertion that reads it back. The suite was spending roughly three
    of its five minutes inside bcrypt, a majority of the whole run, to slow down
    an attacker guessing "admin-password".

    Turning the factor down changes only how many times the key derivation
    loops. Every path a test exercises is the real one: the same hashing
    function, the same stored format, the same verification, the same failure
    when the password is wrong. A verification costs what the hash it checks
    cost to make, so this covers ``checkpw`` too, without touching it.

    Four is bcrypt's own minimum. The one test that measures how long signing in
    takes asks for :func:`production_bcrypt_cost` instead.
    """
    monkeypatch.setattr(user_service, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def production_bcrypt_cost(cheap_password_hashing: None) -> Iterator[None]:
    """Put the shipped cost factor back, for a test about how long a hash takes.

    Depends on the fixture it undoes so pytest orders it afterwards.

    The cached absent-account hash is dropped on the way in and on the way out:
    it is computed once per process and keeps whichever factor was in force at
    the time, so a cheap one left in place would make a missing username answer
    far faster than a real one — which is the very leak the test that wants this
    fixture exists to catch — and an expensive one left behind would charge
    every later test in the worker a fifth of a second.

    The factor is put back by hand rather than left to ``monkeypatch``, and put
    back *before* the cache is dropped, because the order of the two is the
    whole point. Fixtures are torn down in reverse order of setup, so where this
    one sits in a test's argument list decides which other teardowns run while
    the expensive factor is still in force — and a teardown that reaches
    ``authenticate()`` with an unknown username refills the cache. Undoing the
    factor first means that even if something refills it afterwards, it refills
    it cheaply. Relying on ``monkeypatch`` would leave that window open, and
    leave it open in a way that depends on where a future test happens to write
    this fixture's name.
    """
    lowered = user_service.BCRYPT_ROUNDS
    user_service._absent_password_hash.cache_clear()
    user_service.BCRYPT_ROUNDS = _PRODUCTION_BCRYPT_ROUNDS
    try:
        yield
    finally:
        user_service.BCRYPT_ROUNDS = lowered
        user_service._absent_password_hash.cache_clear()


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

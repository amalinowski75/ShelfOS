"""Tests for the database engine/session helpers (app.db).

These cover the bootstrap-adjacent wiring that the request-path tests skip
because they bind their own in-memory engine via a dependency override.
"""

from __future__ import annotations

import pytest
from app import db
from app.models.enums import LocationType
from app.models.location import Location
from app.models.user import User
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select


def _memory_engine() -> Engine:
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_ensure_sqlite_dir_creates_parent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "nested" / "shelfos.db"
    db._ensure_sqlite_dir(f"sqlite:///{target}")
    assert target.parent.is_dir()


def test_ensure_sqlite_dir_ignores_non_file_urls(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A non-sqlite URL and the in-memory URL create nothing (and do not raise).
    db._ensure_sqlite_dir("postgresql://localhost/shelfos")
    db._ensure_sqlite_dir("sqlite:///:memory:")
    assert list(tmp_path.iterdir()) == []


def test_engine_kwargs_by_url() -> None:
    assert db._engine_kwargs("sqlite:///x.db") == {
        "connect_args": {"check_same_thread": False}
    }
    assert db._engine_kwargs("postgresql://localhost/db") == {}


def test_create_db_engine_prefers_argument_then_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    explicit = db.create_db_engine("sqlite://")
    assert isinstance(explicit, Engine)
    assert str(explicit.url) == "sqlite://"

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    assert str(db.create_db_engine().url) == "sqlite://"


def test_init_db_creates_tables() -> None:
    engine = _memory_engine()
    db.init_db(engine)
    # Querying a table only succeeds if it was created.
    with Session(engine) as session:
        assert session.exec(select(User)).all() == []


def test_session_scope_commits_on_success() -> None:
    engine = _memory_engine()
    db.init_db(engine)
    with db.session_scope(engine) as session:
        session.add(Location(type=LocationType.BOX, name="B1"))
    with Session(engine) as session:
        assert [loc.name for loc in session.exec(select(Location)).all()] == ["B1"]


def test_session_scope_rolls_back_on_error() -> None:
    engine = _memory_engine()
    db.init_db(engine)
    with (
        pytest.raises(RuntimeError, match="boom"),
        db.session_scope(engine) as session,
    ):
        session.add(Location(type=LocationType.BOX, name="B1"))
        raise RuntimeError("boom")
    with Session(engine) as session:
        assert session.exec(select(Location)).all() == []


def test_get_session_yields_then_closes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = _memory_engine()
    monkeypatch.setattr(db, "engine", engine)
    generator = db.get_session()
    session = next(generator)
    assert isinstance(session, Session)
    # Exhausting the generator runs the ``with`` cleanup (closing the session).
    with pytest.raises(StopIteration):
        next(generator)


def test_api_get_session_yields_then_closes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The FastAPI dependency (normally overridden in tests) also yields/closes."""
    from app.api import deps

    engine = _memory_engine()
    monkeypatch.setattr(deps, "engine", engine)
    generator = deps.get_session()
    session = next(generator)
    assert isinstance(session, Session)
    with pytest.raises(StopIteration):
        next(generator)

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
from sqlalchemy import inspect
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


def test_init_db_adds_an_index_the_database_is_missing() -> None:
    """ShelfOS has no migrations, so an index added to a model would otherwise
    reach new installations only — working on the developer's fresh database and
    not on the one that actually has the rows."""
    engine = _memory_engine()
    db.init_db(engine)
    named = "ix_audit_log_timestamp_id"
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP INDEX {named}")
    assert named not in {ix["name"] for ix in inspect(engine).get_indexes("audit_log")}

    db.init_db(engine)  # the same call every startup makes

    assert named in {ix["name"] for ix in inspect(engine).get_indexes("audit_log")}


def test_init_db_leaves_existing_indexes_alone() -> None:
    # It creates what is missing and nothing else: rebuilding an index on every
    # start would be a surprise on the one table that grows without bound.
    engine = _memory_engine()
    db.init_db(engine)
    before = inspect(engine).get_indexes("audit_log")
    db.init_db(engine)
    assert inspect(engine).get_indexes("audit_log") == before


def test_the_audit_log_is_indexed_on_every_way_it_is_read() -> None:
    """The one table that grows without bound and is never pruned. Its reads are
    the newest-first walk (which is also its paging key) and the who/kind column
    filters; a scan is cheap on today's log and the page's problem in two years."""
    engine = _memory_engine()
    db.init_db(engine)
    indexed = {
        tuple(ix["column_names"]) for ix in inspect(engine).get_indexes("audit_log")
    }
    assert ("timestamp", "id") in indexed
    assert ("entity_type",) in indexed
    assert ("user_id",) in indexed

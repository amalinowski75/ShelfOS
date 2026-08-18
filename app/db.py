"""Database engine and session management.

Uses SQLite initially (spec §2); the ``DATABASE_URL`` environment variable
allows pointing at PostgreSQL later without code changes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

DEFAULT_DATABASE_URL = "sqlite:///data/shelfos.db"

_SQLITE_FILE_PREFIX = "sqlite:///"


def _ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database."""
    if not database_url.startswith(_SQLITE_FILE_PREFIX):
        return
    path = database_url[len(_SQLITE_FILE_PREFIX) :]
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def _engine_kwargs(database_url: str) -> dict[str, object]:
    """Return engine kwargs appropriate for the given database URL."""
    if database_url.startswith("sqlite"):
        # ``check_same_thread`` is required for SQLite when the connection is
        # shared across threads (e.g. FastAPI's threadpool).
        return {"connect_args": {"check_same_thread": False}}
    return {}


def create_db_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for ShelfOS.

    Args:
        database_url: Connection string; falls back to ``DATABASE_URL`` env var
            and then to a local SQLite file.
        echo: Whether to log emitted SQL statements.
    """
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return create_engine(url, echo=echo, **_engine_kwargs(url))


# Application-wide engine used by the API/UI layers. Tests create their own
# isolated in-memory engines instead of importing this one.
engine: Engine = create_db_engine()


def init_db(target_engine: Engine | None = None) -> None:
    """Create all tables that do not yet exist.

    Creating the SQLite parent directory happens here (not at import time) so
    merely importing the app never writes to disk.
    """
    # Importing the models module registers every table on SQLModel.metadata.
    import app.models  # noqa: F401  (side-effect import)

    active_engine = target_engine or engine
    _ensure_sqlite_dir(str(active_engine.url))
    SQLModel.metadata.create_all(active_engine)
    _create_missing_indexes(active_engine)


def _create_missing_indexes(target_engine: Engine) -> None:
    """Add indexes the models declare but an existing database lacks.

    ``create_all`` skips a table that already exists -- indexes included -- and
    ShelfOS has no migrations, so an index added to a model would reach new
    installations only, and the query it was added for would keep scanning
    everywhere else. That is the worst shape for a performance fix: it works on
    the developer's fresh database and not on the database that actually has the
    rows.

    ``checkfirst`` asks what is already there, so this is a no-op on every
    startup after the one that introduces an index. It creates indexes, never
    drops or rebuilds them: a column change still needs a human.
    """
    for table in SQLModel.metadata.tables.values():
        for index in table.indexes:
            index.create(target_engine, checkfirst=True)


@contextmanager
def session_scope(target_engine: Engine | None = None) -> Iterator[Session]:
    """Provide a transactional session scope.

    Commits on success, rolls back on exception, and always closes the session.
    """
    session = Session(target_engine or engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session (commit handled by callers)."""
    with Session(engine) as session:
        yield session

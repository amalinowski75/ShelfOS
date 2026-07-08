"""Database engine and session management.

Uses SQLite initially (spec §2); the ``DATABASE_URL`` environment variable
allows pointing at PostgreSQL later without code changes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

DEFAULT_DATABASE_URL = "sqlite:///data/shelfos.db"


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
    """Create all tables that do not yet exist."""
    # Importing the models module registers every table on SQLModel.metadata.
    import app.models  # noqa: F401  (side-effect import)

    SQLModel.metadata.create_all(target_engine or engine)


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

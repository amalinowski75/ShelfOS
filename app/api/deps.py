"""FastAPI database-session dependency.

Authentication/authorization dependencies live in :mod:`app.auth.deps`. Tests
override :func:`get_session` to bind the API to an isolated in-memory database.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session

from app.db import engine


def get_session() -> Iterator[Session]:
    """Yield a database session bound to the application engine."""
    with Session(engine) as session:
        yield session

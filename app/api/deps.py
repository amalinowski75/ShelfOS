"""FastAPI dependencies: database session and current user.

In v1.0 there is no authentication; :func:`get_current_user` returns the seeded
"system user" (decision D2). Tests override :func:`get_session` to bind the API
to an isolated in-memory database.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends
from sqlmodel import Session

from app.db import engine
from app.models.user import User
from app.seed import ensure_system_user


def get_session() -> Iterator[Session]:
    """Yield a database session bound to the application engine."""
    with Session(engine) as session:
        yield session


def get_current_user(session: Session = Depends(get_session)) -> User:
    """Return the acting user (the system user until real auth exists, D2)."""
    return ensure_system_user(session)


def get_current_user_id(user: User = Depends(get_current_user)) -> int:
    """Return the acting user's id (always set after persistence)."""
    assert user.id is not None  # noqa: S101  (invariant: seeded user is persisted)
    return user.id

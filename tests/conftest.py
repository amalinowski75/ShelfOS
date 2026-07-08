"""Shared pytest fixtures.

Every test runs against an isolated in-memory SQLite database so tests are fast
and never touch real data. A ``StaticPool`` keeps the single in-memory
connection alive for the duration of a test.
"""

from __future__ import annotations

from collections.abc import Iterator

import app.models  # noqa: F401  (register tables on SQLModel.metadata)
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


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

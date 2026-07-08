"""Shared pytest fixtures.

Every test runs against an isolated in-memory SQLite database so tests are fast
and never touch real data. A ``StaticPool`` keeps the single in-memory
connection alive for the duration of a test.
"""

from __future__ import annotations

from collections.abc import Iterator

import app.models  # noqa: F401  (register tables on SQLModel.metadata)
import pytest
from app.api.deps import get_session
from app.main import create_app
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


@pytest.fixture
def client(engine: Engine) -> Iterator[object]:
    """Provide a FastAPI TestClient bound to the in-memory engine."""
    from fastapi.testclient import TestClient

    app = create_app(create_tables=False)

    def override_get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client

"""Tests for application startup wiring (app.main._bootstrap via the lifespan).

The request-path tests build the app with ``create_tables=False`` and their own
engine, so the real startup sequence — schema creation plus seeding the system
user and bootstrap admin — is exercised here against an isolated engine.
"""

from __future__ import annotations

from app import config
from app.models.user import User
from app.seed import SYSTEM_USER_NAME
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select


def test_bootstrap_creates_schema_and_seeds_accounts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Entering the app's lifespan creates the schema and seeds the accounts."""
    import app.db as db
    import app.main as main

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # _bootstrap() reaches for both module-level engine bindings and init_db().
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(config, "ENV", "development")

    app = main.create_app(create_tables=True)
    with TestClient(app) as client:  # the context-manager entry runs startup
        assert client.get("/health").json() == {"status": "ok"}

    with Session(engine) as session:
        users = session.exec(select(User)).all()
    by_name = {u.name: u for u in users}
    # The system user cannot log in (no hash); the bootstrap admin can.
    assert by_name[SYSTEM_USER_NAME].password_hash is None
    assert by_name[config.ADMIN_USERNAME].password_hash is not None

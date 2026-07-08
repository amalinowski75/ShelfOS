"""Smoke tests validating the project skeleton wiring."""

from __future__ import annotations

import app
from sqlalchemy.engine import Engine
from sqlmodel import Session


def test_package_version() -> None:
    assert app.__version__ == "1.0.0"


def test_engine_fixture_is_usable(engine: Engine) -> None:
    assert engine.dialect.name == "sqlite"


def test_session_fixture_executes_sql(session: Session) -> None:
    from sqlalchemy import text

    assert session.exec(text("SELECT 1")).one()[0] == 1  # type: ignore[call-overload]

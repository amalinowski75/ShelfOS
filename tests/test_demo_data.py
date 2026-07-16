"""Tests for the demo data generator."""

from __future__ import annotations

import pytest
from app import config
from app.demo_data import populate_demo
from app.services import component_service as cs
from app.services import stock_service as ss
from sqlmodel import Session


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """The demo BOM keeps its CSV as an attachment — send it to a tmp dir."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


def test_populate_demo_creates_a_full_dataset(session: Session) -> None:
    counts = populate_demo(session)

    # A few dozen components across several types.
    assert counts["components"] >= 40
    assert counts["types"] >= 6
    assert counts["invoices"] == 2
    assert counts["movements"] > 0
    assert counts["boms"] == 1  # a demo BOM to try the availability report

    # The stock cache stays consistent with the movement ledger (D1).
    assert ss.verify_cache_consistency(session)

    # Type-specific parameters were populated (inheritance path works).
    mosfet = next(t for t in cs.list_types(session) if t.name == "mosfet")
    definitions = cs.get_effective_parameter_definitions(session, mosfet.id)
    assert {d.name for d in definitions} >= {"vds_max", "id_max", "rds_on"}


def test_populate_demo_is_deterministic(session: Session) -> None:
    first = populate_demo(session, seed=42)
    assert first["components"] >= 40

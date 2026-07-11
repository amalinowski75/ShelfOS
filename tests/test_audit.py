"""Tests for audit logging across services (spec §19, decision D9)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from app.models.enums import LocationType, ParameterDataType
from app.seed import ensure_system_user
from app.services import audit_service as audit
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import InsufficientStockError
from fastapi.testclient import TestClient
from sqlmodel import Session


@pytest.fixture
def ctx(session: Session) -> dict[str, int]:
    """A user, component, location and a table parameter for audit tests."""
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
        unit="ohm",
    )
    component = cs.create_component(session, ctype.id)
    location = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    return {
        "user_id": user.id,
        "component_id": component.id,
        "location_id": location.id,
        "definition_id": definition.id,
    }


def test_stock_movement_is_audited(ctx, session: Session) -> None:
    ss.add_stock(
        session,
        component_id=ctx["component_id"],
        location_id=ctx["location_id"],
        quantity=100,
        user_id=ctx["user_id"],
    )
    entries = audit.list_entries(
        session, entity_type="component", entity_id=ctx["component_id"]
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.field == f"quantity@location:{ctx['location_id']}"
    assert entry.old_value == "0"
    assert entry.new_value == "100"
    assert entry.user_id == ctx["user_id"]


def test_failed_movement_writes_no_audit(ctx, session: Session) -> None:
    """A rejected change leaves no audit row (recorded only after it applies)."""
    ss.add_stock(
        session,
        component_id=ctx["component_id"],
        location_id=ctx["location_id"],
        quantity=5,
        user_id=ctx["user_id"],
    )
    with pytest.raises(InsufficientStockError):
        ss.remove_stock(
            session,
            component_id=ctx["component_id"],
            location_id=ctx["location_id"],
            quantity=6,
            user_id=ctx["user_id"],
        )
    entries = audit.list_entries(
        session, entity_type="component", entity_id=ctx["component_id"]
    )
    assert len(entries) == 1  # only the successful add


def test_parameter_change_is_audited_only_with_user(ctx, session: Session) -> None:
    # Without a user id nothing is logged (system/seed context).
    cs.set_parameter_value(
        session, ctx["component_id"], ctx["definition_id"], 4700.0
    )
    assert audit.list_entries(session, entity_type="component") == []

    # With a user, both the first set and a later update are recorded.
    for value in (4700.0, 2200.0):
        cs.set_parameter_value(
            session,
            ctx["component_id"],
            ctx["definition_id"],
            value,
            user_id=ctx["user_id"],
        )
    entries = audit.list_entries(session, entity_type="component")
    assert [e.field for e in entries] == [
        "parameter:resistance",
        "parameter:resistance",
    ]
    # Most recent first: the update from 4700 -> 2200.
    assert entries[0].old_value == "4700.0"
    assert entries[0].new_value == "2200.0"


def test_line_location_change_is_audited(ctx, session: Session) -> None:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    line = inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.set_line_location(
        session, invoice.id, line.id, ctx["location_id"], user_id=ctx["user_id"]
    )
    entries = audit.list_entries(
        session, entity_type="invoice_line", entity_id=line.id
    )
    assert len(entries) == 1
    assert entries[0].field == "location_id"
    assert entries[0].old_value is None
    assert entries[0].new_value == str(ctx["location_id"])


def test_component_deletion_is_audited(ctx, session: Session) -> None:
    """A hard delete leaves an audit row even though the component is gone."""
    cs.hard_delete_component(
        session, ctx["component_id"], user_id=ctx["user_id"]
    )
    entries = audit.list_entries(
        session, entity_type="component", entity_id=ctx["component_id"]
    )
    assert len(entries) == 1
    assert entries[0].field == "deleted"
    assert entries[0].new_value == "true"
    assert entries[0].user_id == ctx["user_id"]


def test_line_removal_is_audited(ctx, session: Session) -> None:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    line = inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.remove_line(session, invoice.id, line.id, user_id=ctx["user_id"])
    entries = audit.list_entries(
        session, entity_type="invoice_line", entity_id=line.id
    )
    assert len(entries) == 1
    assert entries[0].field == "deleted"
    assert entries[0].new_value == "true"


def test_finalization_is_audited(ctx, session: Session) -> None:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=10,
        unit_price=Decimal("1.50"),
        location_id=ctx["location_id"],
    )
    inv.finalize_invoice(session, invoice.id, user_id=ctx["user_id"])

    entries = audit.list_entries(
        session, entity_type="invoice", entity_id=invoice.id
    )
    fields = {e.field: e for e in entries}
    assert fields["is_finalized"].old_value == "false"
    assert fields["is_finalized"].new_value == "true"
    # The prior gross is the real stored zero, not a placeholder ``None``.
    assert fields["total_gross"].old_value == "0.000000"
    assert fields["total_gross"].new_value == "15.000000"


def test_audit_endpoint_is_admin_only(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()
    client.post(
        "/api/stock/add",
        json={
            "component_id": component["id"],
            "location_id": location["id"],
            "quantity": 7,
        },
    )

    resp = client.get("/api/admin/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert any(e["field"].startswith("quantity@location:") for e in body)


def test_audit_endpoint_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.get("/api/admin/audit").status_code == 401


def test_audit_endpoint_forbidden_for_non_admin(
    client: TestClient, anon_client: TestClient
) -> None:
    """A normal (non-admin) user is rejected with 403 by the router guard."""
    client.post(
        "/api/admin/users",
        json={"username": "worker", "password": "pw", "role": "user"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "worker", "password": "pw"}
    ).json()["access_token"]

    resp = anon_client.get(
        "/api/admin/audit", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_audit_endpoint_rejects_out_of_range_limit(client: TestClient) -> None:
    """``limit`` is bounded so a negative/huge value cannot dump the whole log."""
    assert client.get("/api/admin/audit", params={"limit": -1}).status_code == 422
    assert client.get("/api/admin/audit", params={"limit": 0}).status_code == 422
    assert client.get("/api/admin/audit", params={"limit": 5000}).status_code == 422
    assert client.get("/api/admin/audit", params={"limit": 100}).status_code == 200

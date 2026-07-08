"""Integration tests for the FastAPI layer.

The app is bound to the in-memory ``engine`` fixture by overriding the session
dependency, so requests exercise the real services against an isolated database.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.deps import get_session
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    app = create_app(create_tables=False)

    def override_get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_type_parameter_and_component_flow(client: TestClient) -> None:
    # Create a type hierarchy and a parameter on the parent.
    transistor = client.post("/api/types", json={"name": "transistor"}).json()
    mosfet = client.post(
        "/api/types", json={"name": "mosfet", "parent_id": transistor["id"]}
    ).json()
    client.post(
        f"/api/types/{transistor['id']}/parameters",
        json={"name": "package", "label": "Package", "data_type": "text"},
    )
    client.post(
        f"/api/types/{mosfet['id']}/parameters",
        json={
            "name": "rds_on",
            "label": "Rds(on)",
            "data_type": "number",
            "unit": "ohm",
            "sort_order": 1,
        },
    )

    # Effective parameters for mosfet include the inherited one (D3).
    params = client.get(f"/api/types/{mosfet['id']}/parameters").json()
    assert [p["name"] for p in params] == ["package", "rds_on"]

    # Create a component and set a parameter value.
    component = client.post("/api/components", json={"type_id": mosfet["id"]}).json()
    rds = next(p for p in params if p["name"] == "rds_on")
    resp = client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": rds["id"], "value": 0.05},
    )
    assert resp.status_code == 200
    assert resp.json()["value_num"] == 0.05


def test_stock_add_and_insufficient_removal(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()

    add = client.post(
        "/api/stock/add",
        json={
            "component_id": component["id"],
            "location_id": location["id"],
            "quantity": 50,
        },
    )
    assert add.status_code == 201

    quantity = client.get(
        "/api/stock/quantity",
        params={"component_id": component["id"], "location_id": location["id"]},
    ).json()
    assert quantity["quantity"] == 50

    # Removing more than in stock is a 409 conflict.
    too_much = client.post(
        "/api/stock/remove",
        json={
            "component_id": component["id"],
            "location_id": location["id"],
            "quantity": 999,
        },
    )
    assert too_much.status_code == 409


def test_invoice_full_flow(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()

    invoice = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-1",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()
    line = client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={
            "component_id": component["id"],
            "quantity": 100,
            "unit_price": "0.05",
            "location_id": location["id"],
        },
    ).json()
    assert line["total_price"] == "5.000000"

    finalized = client.post(f"/api/invoices/{invoice['id']}/finalize", json={}).json()
    assert finalized["is_finalized"] is True

    # Finalization generated stock at the assigned location.
    quantity = client.get(
        "/api/stock/quantity",
        params={"component_id": component["id"], "location_id": location["id"]},
    ).json()
    assert quantity["quantity"] == 100

    # A finalized invoice is read-only (409).
    conflict = client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={
            "component_id": component["id"],
            "quantity": 1,
            "unit_price": "0.05",
            "location_id": location["id"],
        },
    )
    assert conflict.status_code == 409


def test_admin_delete_component(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()

    deleted = client.delete(f"/api/admin/components/{component['id']}")
    assert deleted.status_code == 204

    # Setting a parameter on the deleted component now 404s.
    missing = client.get(f"/api/components/{component['id']}/parameters")
    assert missing.status_code == 404


def test_not_found_and_validation_mapping(client: TestClient) -> None:
    # Unknown type -> 404 from NotFoundError.
    assert client.post("/api/components", json={"type_id": 999}).status_code == 404

    # Empty type name -> 422 from ValidationError.
    assert client.post("/api/types", json={"name": "  "}).status_code == 422

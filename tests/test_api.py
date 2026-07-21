"""Integration tests for the FastAPI layer.

The app is bound to the in-memory ``engine`` fixture by overriding the session
dependency, so requests exercise the real services against an isolated database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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


def test_create_type_with_parameters_in_one_call(client: TestClient) -> None:
    # A single request creates the type and all its parameter definitions (§13).
    response = client.post(
        "/api/types",
        json={
            "name": "capacitor",
            "parameters": [
                {
                    "name": "capacitance",
                    "label": "Capacitance",
                    "data_type": "number",
                    "unit": "farad",
                },
                {
                    "name": "dielectric",
                    "label": "Dielectric",
                    "data_type": "enum",
                    "enum_values": ["X7R", "C0G"],
                    "sort_order": 1,
                },
            ],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert [p["name"] for p in body["parameters"]] == ["capacitance", "dielectric"]

    # The returned definition ids are usable straight away on a component.
    component = client.post("/api/components", json={"type_id": body["id"]}).json()
    dielectric = next(p for p in body["parameters"] if p["name"] == "dielectric")
    ok = client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": dielectric["id"], "value": "X7R"},
    )
    assert ok.status_code == 200
    bad = client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": dielectric["id"], "value": "NP0"},
    )
    assert bad.status_code == 422


def test_effective_parameters_expose_enum_values(client: TestClient) -> None:
    # Enum choices come back on the read path so a client can render a picker
    # without a second call (spec §13); non-enum parameters carry an empty list.
    body = client.post(
        "/api/types",
        json={
            "name": "capacitor",
            "parameters": [
                {
                    "name": "dielectric",
                    "label": "Dielectric",
                    "data_type": "enum",
                    "enum_values": ["X7R", "C0G", "Y5V"],
                },
                {
                    "name": "capacitance",
                    "label": "Capacitance",
                    "data_type": "number",
                    "unit": "farad",
                    "sort_order": 1,
                },
            ],
        },
    ).json()
    by_name = {p["name"]: p for p in body["parameters"]}
    # The create response already carries the choices in declaration order.
    assert by_name["dielectric"]["enum_values"] == ["X7R", "C0G", "Y5V"]
    assert by_name["capacitance"]["enum_values"] == []

    # The GET effective-parameters path exposes the same shape.
    params = client.get(f"/api/types/{body['id']}/parameters").json()
    fetched = {p["name"]: p for p in params}
    assert fetched["dielectric"]["enum_values"] == ["X7R", "C0G", "Y5V"]
    assert fetched["capacitance"]["enum_values"] == []


def test_effective_parameters_expose_inherited_enum_values(
    client: TestClient,
) -> None:
    # The batched loader must resolve enum tokens across the inherited set, not
    # just a type's own parameters (decision D3): an enum defined on the parent
    # comes back with its choices when fetched via the child's effective set.
    parent = client.post(
        "/api/types",
        json={
            "name": "capacitor",
            "parameters": [
                {
                    "name": "dielectric",
                    "label": "Dielectric",
                    "data_type": "enum",
                    "enum_values": ["X7R", "C0G"],
                }
            ],
        },
    ).json()
    child = client.post(
        "/api/types",
        json={
            "name": "mlcc",
            "parent_id": parent["id"],
            "parameters": [
                {
                    "name": "package",
                    "label": "Package",
                    "data_type": "enum",
                    "enum_values": ["0402", "0603"],
                    "sort_order": 1,
                }
            ],
        },
    ).json()

    params = client.get(f"/api/types/{child['id']}/parameters").json()
    by_name = {p["name"]: p for p in params}
    # Inherited-first ordering, and both the ancestor's and child's enum tokens
    # are present with the correct owners.
    assert [p["name"] for p in params] == ["dielectric", "package"]
    assert by_name["dielectric"]["type_id"] == parent["id"]
    assert by_name["dielectric"]["enum_values"] == ["X7R", "C0G"]
    assert by_name["package"]["type_id"] == child["id"]
    assert by_name["package"]["enum_values"] == ["0402", "0603"]


def test_add_parameter_definition_returns_enum_values(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "led"}).json()
    resp = client.post(
        f"/api/types/{ctype['id']}/parameters",
        json={
            "name": "color",
            "label": "Color",
            "data_type": "enum",
            "enum_values": ["red", "green", "blue"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["enum_values"] == ["red", "green", "blue"]


def test_create_type_with_invalid_parameter_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/types",
        json={
            "name": "capacitor",
            "parameters": [
                {"name": "dielectric", "label": "Dielectric", "data_type": "enum"}
            ],
        },
    )
    assert response.status_code == 422
    # Nothing was created: the type list stays empty.
    assert client.get("/api/types").json() == []


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


def test_invoice_list_and_detail_read(client: TestClient) -> None:
    from decimal import Decimal

    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post(
        "/api/components", json={"type_id": ctype["id"], "mpn": "R-100"}
    ).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()

    older = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-1",
            "invoice_date": "2026-07-01",
            "currency": "EUR",
        },
    ).json()
    newer = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-2",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()
    client.post(
        f"/api/invoices/{newer['id']}/lines",
        json={
            "component_id": component["id"],
            "quantity": 5,
            "unit_price": "1.50",
            "location_id": location["id"],
        },
    )
    client.post(f"/api/invoices/{newer['id']}/finalize", json={})

    # List: newest invoice_date first.
    listing = client.get("/api/invoices").json()
    assert [i["invoice_number"] for i in listing] == ["INV-2", "INV-1"]

    # Filter by finalization state.
    finalized = client.get("/api/invoices", params={"finalized": "true"}).json()
    assert [i["invoice_number"] for i in finalized] == ["INV-2"]
    drafts = client.get("/api/invoices", params={"finalized": "false"}).json()
    assert [i["invoice_number"] for i in drafts] == ["INV-1"]

    # Detail: header, totals and lines resolved to their component.
    detail = client.get(f"/api/invoices/{newer['id']}").json()
    assert detail["is_finalized"] is True
    assert Decimal(str(detail["total_net"])) == Decimal("7.5")
    assert len(detail["lines"]) == 1
    line = detail["lines"][0]
    assert line["quantity"] == 5
    assert Decimal(str(line["unit_price"])) == Decimal("1.5")
    assert line["location_id"] == location["id"]
    assert line["component"]["id"] == component["id"]
    assert line["component"]["mpn"] == "R-100"

    # An empty draft still returns a header with no lines.
    empty = client.get(f"/api/invoices/{older['id']}").json()
    assert empty["lines"] == []


def test_get_unknown_invoice_returns_404(client: TestClient) -> None:
    assert client.get("/api/invoices/9999").status_code == 404


def test_list_invoices_empty_and_requires_auth(
    client: TestClient, anon_client: TestClient
) -> None:
    # Empty database -> empty list, not an error.
    assert client.get("/api/invoices").json() == []
    # Both read endpoints are behind auth.
    assert anon_client.get("/api/invoices").status_code == 401
    assert anon_client.get("/api/invoices/1").status_code == 401


def test_list_invoices_tie_break_by_id_desc(client: TestClient) -> None:
    """Two invoices on the same date fall back to id (newest first)."""
    first = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "A",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()
    second = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "B",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()
    ids = [i["id"] for i in client.get("/api/invoices").json()]
    assert ids == [second["id"], first["id"]]


def test_invoice_detail_survives_deleted_component(client: TestClient) -> None:
    """Hard-deleting a component leaves its invoice line readable (component null)."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post(
        "/api/components", json={"type_id": ctype["id"]}
    ).json()
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
    client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={
            "component_id": component["id"],
            "quantity": 2,
            "unit_price": "1.00",
            "location_id": location["id"],
        },
    )
    client.post(f"/api/invoices/{invoice['id']}/finalize", json={})

    assert client.delete(f"/api/admin/components/{component['id']}").status_code == 204

    detail = client.get(f"/api/invoices/{invoice['id']}")
    assert detail.status_code == 200
    line = detail.json()["lines"][0]
    assert line["component_id"] == component["id"]
    assert line["component"] is None


def test_update_invoice_and_line_via_api(client: TestClient) -> None:
    from decimal import Decimal

    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
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
        json={"component_id": component["id"], "quantity": 10, "unit_price": "1.00"},
    ).json()

    # PATCH metadata (partial: number/date/currency untouched).
    patched = client.patch(
        f"/api/invoices/{invoice['id']}",
        json={"supplier": "Digikey", "notes": "rush"},
    )
    assert patched.status_code == 200
    assert patched.json()["supplier"] == "Digikey"
    assert patched.json()["invoice_number"] == "INV-1"

    # PUT line edit recomputes total and net.
    put = client.put(
        f"/api/invoices/{invoice['id']}/lines/{line['id']}",
        json={"quantity": 5, "unit_price": "2.00"},
    )
    assert put.status_code == 200

    detail = client.get(f"/api/invoices/{invoice['id']}").json()
    assert detail["supplier"] == "Digikey"
    assert detail["notes"] == "rush"
    assert Decimal(str(detail["total_net"])) == Decimal("10")
    edited = detail["lines"][0]
    assert edited["quantity"] == 5
    assert Decimal(str(edited["unit_price"])) == Decimal("2")


def test_edit_finalized_invoice_is_conflict(client: TestClient) -> None:
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
            "quantity": 1,
            "unit_price": "1.00",
            "location_id": location["id"],
        },
    ).json()
    client.post(f"/api/invoices/{invoice['id']}/finalize", json={})

    # A finalized invoice is read-only -> 409 from InvoiceFinalizedError.
    assert (
        client.patch(
            f"/api/invoices/{invoice['id']}", json={"supplier": "X"}
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/api/invoices/{invoice['id']}/lines/{line['id']}",
            json={"quantity": 2},
        ).status_code
        == 409
    )


def test_invoice_edit_endpoints_forbidden_for_read_only(
    client: TestClient, anon_client: TestClient
) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
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
        json={"component_id": component["id"], "quantity": 1, "unit_price": "1.00"},
    ).json()

    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "pw", "role": "read-only"},
    )
    token = client.post(
        "/api/auth/token", json={"username": "viewer", "password": "pw"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Read-only accounts are blocked from writes by the router guard.
    assert (
        anon_client.patch(
            f"/api/invoices/{invoice['id']}",
            json={"supplier": "X"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        anon_client.put(
            f"/api/invoices/{invoice['id']}/lines/{line['id']}",
            json={"quantity": 2},
            headers=headers,
        ).status_code
        == 403
    )


def test_update_invoice_db_constraint_maps_to_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``(supplier, number)`` collision that slips past the app-level check must
    surface as 409, not a raw 500 from the ``uq_invoice_supplier_number`` violation.

    Disabling the pre-check simulates the race the handler exists for (two writes
    passing the check before either commits), so the DB constraint is what fires.
    """
    from app.services import invoice_service

    first = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-1",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()
    second = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-2",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()

    # Bypass the app-level guard so the rename reaches the database untouched.
    monkeypatch.setattr(invoice_service, "_number_conflicts", lambda *a, **k: False)

    conflict = client.patch(
        f"/api/invoices/{second['id']}",
        json={"invoice_number": first["invoice_number"]},
    )
    assert conflict.status_code == 409


def test_invoice_line_subresource_endpoints(client: TestClient) -> None:
    """Exercise the list-lines, set-location and delete-line line endpoints."""
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
        json={"component_id": component["id"], "quantity": 2, "unit_price": "1.00"},
    ).json()

    listed = client.get(f"/api/invoices/{invoice['id']}/lines")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [line["id"]]

    located = client.put(
        f"/api/invoices/{invoice['id']}/lines/{line['id']}/location",
        json={"location_id": location["id"]},
    )
    assert located.status_code == 200
    assert located.json()["location_id"] == location["id"]

    assert (
        client.delete(
            f"/api/invoices/{invoice['id']}/lines/{line['id']}"
        ).status_code
        == 204
    )
    assert client.get(f"/api/invoices/{invoice['id']}/lines").json() == []


def test_stock_correction_and_total_endpoints(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()

    correction = client.post(
        "/api/stock/correct",
        json={
            "component_id": component["id"],
            "location_id": location["id"],
            "delta": 7,
            "note": "stock take",
        },
    )
    assert correction.status_code == 201

    total = client.get("/api/stock/total", params={"component_id": component["id"]})
    assert total.status_code == 200
    assert total.json() == {"component_id": component["id"], "quantity": 7}


def test_location_path_endpoint(client: TestClient) -> None:
    parent = client.post(
        "/api/locations", json={"type": "room", "name": "Lab"}
    ).json()
    child = client.post(
        "/api/locations",
        json={"type": "drawer", "name": "D1", "parent_id": parent["id"]},
    ).json()

    path = client.get(f"/api/locations/{child['id']}/path")
    assert path.status_code == 200
    assert [loc["name"] for loc in path.json()] == ["Lab", "D1"]


def test_set_number_parameter_accepts_engineering_string(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        f"/api/types/{ctype['id']}/parameters",
        json={
            "name": "resistance",
            "label": "Resistance",
            "data_type": "number",
            "unit": "Ω",
        },
    )
    definition = client.get(f"/api/types/{ctype['id']}/parameters").json()[0]
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()

    ok = client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": definition["id"], "value": "4k7"},
    )
    assert ok.status_code == 200
    assert ok.json()["value_num"] == 4700.0

    # An unparseable value is a clean 422, not a silently-wrong number.
    bad = client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": definition["id"], "value": "not a number"},
    )
    assert bad.status_code == 422

    # JSON `Infinity` is accepted by the parser/Pydantic but must not be stored.
    inf = client.put(
        f"/api/components/{component['id']}/parameters",
        content=(
            f'{{"parameter_definition_id": {definition["id"]}, "value": Infinity}}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert inf.status_code == 422


def test_create_component_with_nested_parameters(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        f"/api/types/{ctype['id']}/parameters",
        json={
            "name": "resistance",
            "label": "Resistance",
            "data_type": "number",
            "unit": "Ω",
        },
    )
    definition = client.get(f"/api/types/{ctype['id']}/parameters").json()[0]

    resp = client.post(
        "/api/components",
        json={
            "type_id": ctype["id"],
            "mpn": "R-100",
            "parameters": [
                {"parameter_definition_id": definition["id"], "value": "4k7"}
            ],
        },
    )
    assert resp.status_code == 201
    component_id = resp.json()["id"]

    params = client.get(f"/api/components/{component_id}/parameters").json()
    assert params[0]["value_num"] == 4700.0


def test_create_component_with_bad_parameter_is_atomic(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    before = len(client.get("/web/api/components").json()["data"])

    resp = client.post(
        "/api/components",
        json={
            "type_id": ctype["id"],
            "parameters": [{"parameter_definition_id": 9999, "value": "1k"}],
        },
    )
    assert resp.status_code == 422
    # The whole create was rolled back — no component landed.
    after = len(client.get("/web/api/components").json()["data"])
    assert after == before


def test_create_component_with_wrong_typed_value_is_atomic(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        f"/api/types/{ctype['id']}/parameters",
        json={
            "name": "resistance",
            "label": "Resistance",
            "data_type": "number",
            "unit": "Ω",
        },
    )
    definition = client.get(f"/api/types/{ctype['id']}/parameters").json()[0]
    before = len(client.get("/web/api/components").json()["data"])

    # A value that fails _assign_value (unparseable number) aborts the create.
    resp = client.post(
        "/api/components",
        json={
            "type_id": ctype["id"],
            "parameters": [
                {"parameter_definition_id": definition["id"], "value": "not a number"}
            ],
        },
    )
    assert resp.status_code == 422
    after = len(client.get("/web/api/components").json()["data"])
    assert after == before


def _account_headers(
    client: TestClient,
    anon_client: TestClient,
    *,
    username: str = "carol",
    password: str = "oldpassword",
    role: str = "user",
) -> dict[str, str]:
    """Create an account (via the admin client) and return its bearer header."""
    client.post(
        "/api/admin/users",
        json={"username": username, "password": password, "role": role},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_user_can_change_own_password(
    client: TestClient, anon_client: TestClient
) -> None:
    headers = _account_headers(client, anon_client)
    resp = anon_client.post(
        "/api/auth/change-password",
        json={"current_password": "oldpassword", "new_password": "newpassword"},
        headers=headers,
    )
    assert resp.status_code == 200
    # The old password stops working and the new one is accepted.
    assert (
        anon_client.post(
            "/api/auth/token", json={"username": "carol", "password": "oldpassword"}
        ).status_code
        == 401
    )
    assert (
        anon_client.post(
            "/api/auth/token", json={"username": "carol", "password": "newpassword"}
        ).status_code
        == 200
    )


def test_change_password_rejects_wrong_current(
    client: TestClient, anon_client: TestClient
) -> None:
    headers = _account_headers(client, anon_client)
    resp = anon_client.post(
        "/api/auth/change-password",
        json={"current_password": "not-it", "new_password": "newpassword"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "current password" in resp.json()["detail"].lower()
    # The password is unchanged.
    assert (
        anon_client.post(
            "/api/auth/token", json={"username": "carol", "password": "oldpassword"}
        ).status_code
        == 200
    )


def test_read_only_user_can_change_own_password(
    client: TestClient, anon_client: TestClient
) -> None:
    # The endpoint is off the read-only write block, so a viewer can rotate creds.
    headers = _account_headers(client, anon_client, username="viewer", role="read-only")
    resp = anon_client.post(
        "/api/auth/change-password",
        json={"current_password": "oldpassword", "new_password": "newpassword"},
        headers=headers,
    )
    assert resp.status_code == 200


def test_change_password_rejects_empty_new(
    client: TestClient, anon_client: TestClient
) -> None:
    headers = _account_headers(client, anon_client)
    resp = anon_client.post(
        "/api/auth/change-password",
        json={"current_password": "oldpassword", "new_password": ""},
        headers=headers,
    )
    assert resp.status_code == 422


def test_change_password_requires_authentication(anon_client: TestClient) -> None:
    resp = anon_client.post(
        "/api/auth/change-password",
        json={"current_password": "x", "new_password": "y"},
    )
    assert resp.status_code == 401


def _resistor_type_id(client: TestClient) -> int:
    return client.post("/api/types", json={"name": "resistor"}).json()["id"]


def test_create_duplicate_component_is_blocked_with_a_link(client: TestClient) -> None:
    type_id = _resistor_type_id(client)
    first = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "R-100", "manufacturer": "YAGEO"},
    )
    assert first.status_code == 201
    existing_id = first.json()["id"]

    # A second create with the same (MPN, manufacturer) — any case — is refused.
    dup = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "r-100", "manufacturer": "yageo"},
    )
    assert dup.status_code == 409  # Conflict
    body = dup.json()
    assert body["existing_id"] == existing_id  # so the UI can link to it
    assert "already exists" in body["detail"]


def test_same_mpn_different_manufacturer_is_allowed(client: TestClient) -> None:
    type_id = _resistor_type_id(client)
    client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "R-100", "manufacturer": "YAGEO"},
    )
    other = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "R-100", "manufacturer": "TDK"},
    )
    assert other.status_code == 201


def test_components_without_an_mpn_do_not_collide(client: TestClient) -> None:
    type_id = _resistor_type_id(client)
    a = client.post("/api/components", json={"type_id": type_id})
    b = client.post("/api/components", json={"type_id": type_id})
    assert a.status_code == 201 and b.status_code == 201  # two MPN-less parts coexist


def test_duplicate_check_ignores_case_whitespace_and_blank_manufacturer(
    client: TestClient,
) -> None:
    type_id = _resistor_type_id(client)
    # An empty-string manufacturer normalises to None, same as omitting it.
    client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "R-1", "manufacturer": ""},
    )
    # A whitespace-padded, differently-cased re-add of the same blank-manufacturer
    # part is still caught.
    dup = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "  r-1  "},
    )
    assert dup.status_code == 409


def test_a_deleted_component_frees_its_mpn_for_re_add(client: TestClient) -> None:
    type_id = _resistor_type_id(client)
    created = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "R-2", "manufacturer": "YAGEO"},
    ).json()
    # After the part is deleted (admin), the same (MPN, manufacturer) may be added
    # again — the duplicate check must not count a removed component. (The
    # deleted_at filter itself is unit-tested against a soft-deleted row.)
    assert client.delete(f"/api/admin/components/{created['id']}").status_code == 204
    again = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "R-2", "manufacturer": "YAGEO"},
    )
    assert again.status_code == 201

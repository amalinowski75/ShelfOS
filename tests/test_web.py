"""Integration tests for the server-rendered web UI."""

from __future__ import annotations

from app.models.component import ComponentParameter, ParameterDefinition
from app.models.enums import ParameterDataType
from app.web.presenter import build_component_table, format_parameter_value
from fastapi.testclient import TestClient


def _definition(data_type: ParameterDataType, unit: str | None = None):
    return ParameterDefinition(
        type_id=1, name="p", label="P", data_type=data_type, unit=unit
    )


def test_format_parameter_value_variants() -> None:
    assert format_parameter_value(_definition(ParameterDataType.TEXT), None) == ""

    num = _definition(ParameterDataType.NUMBER, unit="F")
    assert format_parameter_value(num, ComponentParameter(value_num=1e-7)) == "100 nF"
    assert format_parameter_value(num, ComponentParameter(value_num=None)) == ""

    boolean = _definition(ParameterDataType.BOOL)
    assert format_parameter_value(boolean, ComponentParameter(value_bool=True)) == "yes"
    assert format_parameter_value(boolean, ComponentParameter(value_bool=False)) == "no"

    text = _definition(ParameterDataType.TEXT)
    assert format_parameter_value(text, ComponentParameter(value_text="red")) == "red"

    enum = _definition(ParameterDataType.ENUM)
    assert format_parameter_value(enum, ComponentParameter(value_text="X7R")) == "X7R"


def test_index_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Components" in response.text
    # Static assets and Tabulator init are wired up.
    assert "/static/app.js" in response.text


def test_components_feed_generic_view(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        "/api/components",
        json={"type_id": ctype["id"], "manufacturer": "Yageo", "mpn": "RC0603"},
    )

    feed = client.get("/web/api/components").json()
    fields = [c["field"] for c in feed["columns"]]
    assert fields == [
        "type",
        "manufacturer",
        "mpn",
        "package",
        "mounting_type",
        "quantity",
    ]
    assert len(feed["data"]) == 1
    assert feed["data"][0]["manufacturer"] == "Yageo"


def test_components_feed_type_specific_adds_parameter_columns(
    client: TestClient,
) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        f"/api/types/{ctype['id']}/parameters",
        json={
            "name": "resistance",
            "label": "Resistance",
            "data_type": "number",
            "unit": "ohm",
            "is_table_column": True,
        },
    )
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    definition = client.get(f"/api/types/{ctype['id']}/parameters").json()[0]
    client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": definition["id"], "value": 4700},
    )

    feed = client.get("/web/api/components", params={"type_id": ctype["id"]}).json()
    titles = [c["title"] for c in feed["columns"]]
    assert "Resistance" in titles
    # Engineering-formatted value with the definition's unit (decision D4).
    assert feed["data"][0][f"param_{definition['id']}"] == "4.7 kohm"


def test_component_detail_page(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post(
        "/api/components", json={"type_id": ctype["id"], "mpn": "RC0603"}
    ).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()
    client.post(
        "/api/stock/add",
        json={
            "component_id": component["id"],
            "location_id": location["id"],
            "quantity": 25,
        },
    )

    response = client.get(f"/components/{component['id']}")
    assert response.status_code == 200
    assert "RC0603" in response.text
    assert "Stock by location" in response.text
    assert "D1" in response.text


def test_component_detail_missing_returns_404(client: TestClient) -> None:
    assert client.get("/components/999").status_code == 404


def test_build_component_table_empty(session) -> None:  # type: ignore[no-untyped-def]
    payload = build_component_table(session)
    assert payload["data"] == []
    assert [c["field"] for c in payload["columns"]][0] == "type"

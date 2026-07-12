"""Integration tests for the server-rendered web UI."""

from __future__ import annotations

import pytest
from app.models.component import ComponentParameter, ParameterDefinition
from app.models.enums import ParameterDataType
from app.web.presenter import (
    build_component_table,
    format_money,
    format_parameter_value,
)
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


def test_index_shows_new_type_control_for_writer(client: TestClient) -> None:
    """An account that can write sees the §13 create-type dialog and builder."""
    html = client.get("/").text
    assert 'id="new-type-btn"' in html
    assert 'id="type-dialog"' in html
    assert 'id="param-row-template"' in html
    # The parameter builder offers every data type, enum included.
    assert 'value="enum"' in html
    # An inherited-parameters panel lets the user see what a parent already
    # defines before adding duplicates (spec §13, D3).
    assert 'id="inherited-list"' in html


def test_require_web_user_heals_missing_csrf_token(session) -> None:  # type: ignore[no-untyped-def]
    """An authenticated session without a CSRF token gets one issued on render.

    Guards the regression where a pre-CSRF (or older-build) session cookie kept
    authenticating but left the meta token empty, so every browser write 403'd.
    """
    from app.models.enums import UserRole
    from app.services import user_service as us
    from app.web.routes import require_web_user
    from starlette.requests import Request

    user = us.create_user(
        session, username="stale", password="pw", role=UserRole.USER
    )
    # A session that authenticates (user_id) but predates CSRF (no token).
    sess: dict[str, object] = {"user_id": user.id}
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [],
        "session": sess,
        "state": {},
    }
    result = require_web_user(Request(scope), session)
    assert result.id == user.id
    assert sess.get("csrf_token")  # a fresh token was healed into the session


def test_create_type_via_web_session_requires_csrf(
    session,  # type: ignore[no-untyped-def]
    anon_client: TestClient,
) -> None:
    """End-to-end browser path: form login, then a create-type write with CSRF."""
    import re

    from app.models.enums import UserRole
    from app.services import user_service as us

    us.create_user(session, username="admin", password="admin", role=UserRole.ADMIN)
    anon_client.post("/login", data={"username": "admin", "password": "admin"})

    html = anon_client.get("/").text
    token = re.search(r'name="csrf-token" content="([^"]*)"', html).group(1)  # type: ignore[union-attr]
    assert token  # the page exposes a usable token

    ok = anon_client.post(
        "/api/types", json={"name": "resistor"}, headers={"X-CSRF-Token": token}
    )
    assert ok.status_code == 201
    # The same write without the token is rejected by the CSRF guard.
    missing = anon_client.post("/api/types", json={"name": "capacitor"})
    assert missing.status_code == 403


def test_create_type_accepts_builder_shaped_payload(
    session,  # type: ignore[no-untyped-def]
    anon_client: TestClient,
) -> None:
    """The exact JSON the dialog's builder emits round-trips through the API.

    Mirrors ``collectParameters()`` in app.js (unit ``None``, per-row
    ``sort_order``, ``enum_values`` only on the enum row) so a schema drift
    between the builder and ``TypeCreate`` would fail here.
    """
    import re

    from app.models.enums import UserRole
    from app.services import user_service as us

    us.create_user(session, username="admin", password="admin", role=UserRole.ADMIN)
    anon_client.post("/login", data={"username": "admin", "password": "admin"})
    token = re.search(  # type: ignore[union-attr]
        r'name="csrf-token" content="([^"]*)"', anon_client.get("/").text
    ).group(1)

    payload = {
        "name": "capacitor",
        "parent_id": None,
        "parameters": [
            {
                "name": "capacitance",
                "label": "Capacitance",
                "data_type": "number",
                "unit": "farad",
                "is_table_column": True,
                "is_filterable": False,
                "sort_order": 0,
            },
            {
                "name": "dielectric",
                "label": "Dielectric",
                "data_type": "enum",
                "unit": None,
                "is_table_column": False,
                "is_filterable": True,
                "sort_order": 1,
                "enum_values": ["X7R", "C0G"],
            },
        ],
    }
    resp = anon_client.post(
        "/api/types", json=payload, headers={"X-CSRF-Token": token}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert [p["name"] for p in body["parameters"]] == ["capacitance", "dielectric"]
    dielectric = next(p for p in body["parameters"] if p["name"] == "dielectric")
    assert dielectric["enum_values"] == ["X7R", "C0G"]


def test_new_type_control_hidden_for_read_only(client: TestClient) -> None:
    """A read-only account cannot write, so the create-type control is absent."""
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "pw", "role": "read-only"},
    )
    token = client.post(
        "/api/auth/token", json={"username": "viewer", "password": "pw"}
    ).json()["access_token"]

    html = client.get("/", headers={"Authorization": f"Bearer {token}"}).text
    assert "New Type" not in html
    assert 'id="new-type-btn"' not in html


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


def test_components_feed_maps_values_per_component(client: TestClient) -> None:
    """Batched parameter loading keeps each row's value with its own component (L5)."""
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
    definition = client.get(f"/api/types/{ctype['id']}/parameters").json()[0]

    ids = []
    for value in (4700, 1000000):
        comp = client.post("/api/components", json={"type_id": ctype["id"]}).json()
        ids.append(comp["id"])
        client.put(
            f"/api/components/{comp['id']}/parameters",
            json={"parameter_definition_id": definition["id"], "value": value},
        )

    feed = client.get("/web/api/components", params={"type_id": ctype["id"]}).json()
    by_id = {row["id"]: row[f"param_{definition['id']}"] for row in feed["data"]}
    assert by_id[ids[0]] == "4.7 kohm"
    assert by_id[ids[1]] == "1 Mohm"


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


def _seed_admin(session) -> None:  # type: ignore[no-untyped-def]
    from app.models.enums import UserRole
    from app.services import user_service as us

    us.create_user(session, username="admin", password="admin", role=UserRole.ADMIN)


def test_web_pages_require_login(anon_client: TestClient) -> None:
    resp = anon_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_page_renders(anon_client: TestClient) -> None:
    resp = anon_client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_login_flow_grants_access(session, anon_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    _seed_admin(session)
    resp = anon_client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # The session cookie now grants access to protected pages.
    assert anon_client.get("/", follow_redirects=False).status_code == 200


def test_login_invalid_credentials(session, anon_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    resp = anon_client.post("/login", data={"username": "ghost", "password": "x"})
    assert resp.status_code == 401
    assert "Invalid" in resp.text


def test_logout_clears_session(session, anon_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    _seed_admin(session)
    anon_client.post("/login", data={"username": "admin", "password": "admin"})
    anon_client.post("/logout", follow_redirects=False)
    assert anon_client.get("/", follow_redirects=False).status_code == 303


def _invoice_with_line(client: TestClient) -> dict[str, object]:
    """Create a type, component, location and a one-line invoice; return handles."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post(
        "/api/components", json={"type_id": ctype["id"], "mpn": "R-100"}
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
            "quantity": 5,
            "unit_price": "1.50",
            "supplier_part_number": "SPN-9",
            "location_id": location["id"],
        },
    )
    return {"invoice": invoice, "component": component}


def test_invoices_list_page_renders(client: TestClient) -> None:
    _invoice_with_line(client)
    # A second, later invoice to confirm ordering (newest invoice_date first).
    client.post(
        "/api/invoices",
        json={
            "supplier": "Digikey",
            "invoice_number": "INV-2",
            "invoice_date": "2026-07-10",
            "currency": "USD",
        },
    )
    html = client.get("/invoices").text
    assert "Invoices" in html
    assert 'href="/invoices/' in html
    # Newest invoice_date first: INV-2 appears before INV-1 in the markup.
    assert html.index("INV-2") < html.index("INV-1")


def test_invoices_list_page_empty(client: TestClient) -> None:
    html = client.get("/invoices").text
    assert "No invoices yet." in html


def test_invoice_detail_page_links_to_components(client: TestClient) -> None:
    handles = _invoice_with_line(client)
    invoice = handles["invoice"]
    component = handles["component"]
    html = client.get(f"/invoices/{invoice['id']}").text  # type: ignore[index]
    # Header and line data are present.
    assert "INV-1" in html
    assert "Mouser" in html
    assert "SPN-9" in html
    assert "D1" in html  # the line's location path
    # Each line links to its component (invoice -> component navigation, §9).
    assert f'href="/components/{component["id"]}"' in html  # type: ignore[index]
    assert "R-100" in html


def test_invoice_detail_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/invoices/9999").status_code == 404


def test_component_detail_links_to_invoices(client: TestClient) -> None:
    """Purchase history links back to each invoice (component -> invoice, §9)."""
    handles = _invoice_with_line(client)
    invoice = handles["invoice"]
    component = handles["component"]
    # Finalize so the line shows up as purchase history.
    client.post(f"/api/invoices/{invoice['id']}/finalize", json={})  # type: ignore[index]
    html = client.get(f"/components/{component['id']}").text  # type: ignore[index]
    assert f'href="/invoices/{invoice["id"]}"' in html  # type: ignore[index]


def test_invoice_detail_money_has_no_trailing_zeros(client: TestClient) -> None:
    """Amounts render as ``1.50``, not the stored six-place ``1.500000``."""
    handles = _invoice_with_line(client)
    invoice = handles["invoice"]
    html = client.get(f"/invoices/{invoice['id']}").text  # type: ignore[index]
    assert "1.50 EUR" in html
    assert "1.500000" not in html
    # A draft's gross is not shown as a computed 0 (it is set on finalize).
    assert "set on finalize" in html


def test_invoice_detail_handles_deleted_component(
    client: TestClient,
    session,  # type: ignore[no-untyped-def]
) -> None:
    """A line whose component was hard-deleted degrades to a muted label, not 500."""
    handles = _invoice_with_line(client)
    invoice = handles["invoice"]
    component = handles["component"]

    # Hard-delete the component directly; §20 keeps the invoice line as history.
    from app.models.component import Component

    obj = session.get(Component, component["id"])  # type: ignore[index]
    session.delete(obj)
    session.commit()

    resp = client.get(f"/invoices/{invoice['id']}")  # type: ignore[index]
    assert resp.status_code == 200
    assert "(deleted)" in resp.text


def test_invoice_detail_empty_and_unlocated_line(client: TestClient) -> None:
    """A draft with a line but no location renders the em-dash placeholder."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    invoice = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-Z",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()

    # No lines yet.
    assert "This invoice has no lines." in client.get(
        f"/invoices/{invoice['id']}"
    ).text

    # A line without a location shows the placeholder rather than crashing.
    client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={"component_id": component["id"], "quantity": 1, "unit_price": "2"},
    )
    assert client.get(f"/invoices/{invoice['id']}").status_code == 200


def test_invoice_list_hint_when_truncated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the list hits its cap, the page says so instead of dropping rows."""
    from app.web import routes

    monkeypatch.setattr(routes, "_INVOICE_LIST_LIMIT", 1)
    for n in range(2):
        client.post(
            "/api/invoices",
            json={
                "supplier": "Mouser",
                "invoice_number": f"INV-{n}",
                "invoice_date": "2026-07-08",
                "currency": "EUR",
            },
        )
    assert "1 most recent invoices" in client.get("/invoices").text


def test_invoice_pages_require_login(anon_client: TestClient) -> None:
    """Both new invoice pages redirect an anonymous visitor to login."""
    for path in ("/invoices", "/invoices/1"):
        resp = anon_client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_format_money_strips_trailing_zeros() -> None:
    from decimal import Decimal

    assert format_money(Decimal("1.500000")) == "1.50"
    assert format_money(Decimal("7.500000")) == "7.50"
    assert format_money(Decimal("0.000000")) == "0.00"
    assert format_money(Decimal("0.001234")) == "0.001234"
    assert format_money(Decimal("100")) == "100.00"


def _read_only_headers(client: TestClient) -> dict[str, str]:
    """Create a read-only account and return its bearer auth header."""
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "pw", "role": "read-only"},
    )
    token = client.post(
        "/api/auth/token", json={"username": "viewer", "password": "pw"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_authenticated_pages_load_shared_js(client: TestClient) -> None:
    """The shared JS helpers are served ahead of each page's own script."""
    invoice = _invoice_with_line(client)["invoice"]
    assert "/static/shared.js" in client.get("/").text
    assert "/static/shared.js" in client.get("/invoices").text
    assert "/static/shared.js" in client.get(
        f"/invoices/{invoice['id']}"  # type: ignore[index]
    ).text


def test_invoice_list_new_button_writer_only(client: TestClient) -> None:
    """A writer sees the create-invoice control; a read-only account does not."""
    writer_html = client.get("/invoices").text
    assert 'id="invoice-new-btn"' in writer_html
    assert 'id="invoice-new-dialog"' in writer_html
    assert "/static/invoices.js" in writer_html

    reader_html = client.get("/invoices", headers=_read_only_headers(client)).text
    assert 'id="invoice-new-btn"' not in reader_html
    assert 'id="invoice-new-dialog"' not in reader_html


def test_invoice_detail_draft_shows_write_controls(client: TestClient) -> None:
    """A draft offers metadata edit, line add/edit/remove and finalize (§16)."""
    invoice = _invoice_with_line(client)["invoice"]
    html = client.get(f"/invoices/{invoice['id']}").text  # type: ignore[index]
    assert 'id="invoice-edit-btn"' in html
    assert 'id="invoice-addline-btn"' in html
    assert 'id="invoice-finalize-btn"' in html
    assert 'id="invoice-line-dialog"' in html
    assert 'data-act="edit-line"' in html
    assert 'data-act="remove-line"' in html
    # The row carries the data the edit dialog prefills from.
    assert 'data-line-id="' in html
    assert 'data-unit-price="1.50"' in html


def test_invoice_detail_finalized_is_read_only(client: TestClient) -> None:
    """Once finalized, the page drops every write control and its dialogs."""
    invoice = _invoice_with_line(client)["invoice"]
    client.post(f"/api/invoices/{invoice['id']}/finalize", json={})  # type: ignore[index]
    html = client.get(f"/invoices/{invoice['id']}").text  # type: ignore[index]
    for marker in (
        'id="invoice-edit-btn"',
        'id="invoice-addline-btn"',
        'id="invoice-finalize-btn"',
        'id="invoice-meta-dialog"',
        'id="invoice-line-dialog"',
        'id="invoice-finalize-dialog"',
        'data-act="edit-line"',
        'data-act="remove-line"',
    ):
        assert marker not in html, marker


def test_invoice_detail_finalize_hidden_without_lines(client: TestClient) -> None:
    """Finalize is offered only once the draft has a line (it is else rejected)."""
    invoice = client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-EMPTY",
            "invoice_date": "2026-07-08",
            "currency": "EUR",
        },
    ).json()
    empty_html = client.get(f"/invoices/{invoice['id']}").text
    assert 'id="invoice-finalize-btn"' not in empty_html
    assert 'id="invoice-finalize-dialog"' not in empty_html
    # Adding a line brings the control back.
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={"component_id": component["id"], "quantity": 1, "unit_price": "1"},
    )
    assert 'id="invoice-finalize-btn"' in client.get(f"/invoices/{invoice['id']}").text


def test_invoice_detail_read_only_account_has_no_controls(
    client: TestClient,
) -> None:
    """A read-only account viewing a draft still sees no write controls."""
    invoice = _invoice_with_line(client)["invoice"]
    html = client.get(
        f"/invoices/{invoice['id']}",  # type: ignore[index]
        headers=_read_only_headers(client),
    ).text
    assert 'id="invoice-edit-btn"' not in html
    assert 'data-act="edit-line"' not in html
    assert 'id="invoice-line-dialog"' not in html

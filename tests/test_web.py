"""Integration tests for the server-rendered web UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import config
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
    assert format_parameter_value(boolean, ComponentParameter(value_bool=None)) == ""

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


def test_stock_dialog_uses_location_tree_picker(client: TestClient) -> None:
    """The stock dialog's location field is the expandable tree-picker (§7)."""
    room = client.post(
        "/api/locations", json={"type": "room", "name": "Lab"}
    ).json()
    client.post(
        "/api/locations",
        json={"type": "rack", "name": "Rack A", "parent_id": room["id"]},
    )
    html = client.get("/").text
    assert 'class="loc-picker"' in html
    # Nodes carry the id and full path the widget selects/labels with.
    assert 'data-loc-path="Lab / Rack A"' in html
    assert "/static/location_tree.js" in html
    # The picker offers inline location creation via the shared New Location dialog.
    assert 'class="loc-picker-new"' in html
    assert 'id="location-dialog"' in html


def test_index_shows_new_type_control_for_writer(client: TestClient) -> None:
    """An account that can write sees the §13 create-type dialog and builder."""
    html = client.get("/").text
    # The role meta drives client-side write gating (admin here → writer).
    assert 'name="user-role" content="admin"' in html
    assert 'id="new-type-btn"' in html
    assert 'id="type-dialog"' in html
    assert 'id="param-row-template"' in html
    # The parameter builder offers every data type, enum included.
    assert 'value="enum"' in html
    # An inherited-parameters panel lets the user see what a parent already
    # defines before adding duplicates (spec §13, D3).
    assert 'id="inherited-list"' in html


def test_index_shows_new_component_control_for_writer(client: TestClient) -> None:
    """A writer sees the §16.5 create-component dialog with its base fields."""
    html = client.get("/").text
    assert 'id="new-component-btn"' in html
    assert 'id="component-dialog"' in html
    assert 'id="component-type"' in html
    assert 'id="component-params"' in html
    # The mounting select offers the enum values.
    assert 'value="THT"' in html


def test_new_component_control_hidden_for_read_only(client: TestClient) -> None:
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "pw", "role": "read-only"},
    )
    token = client.post(
        "/api/auth/token", json={"username": "viewer", "password": "pw"}
    ).json()["access_token"]
    html = client.get("/", headers={"Authorization": f"Bearer {token}"}).text
    assert 'id="new-component-btn"' not in html
    # The dialog markup itself is gated too, not just the button.
    assert 'id="component-dialog"' not in html
    # The stock dialog (and so its inline New Location dialog) is gated as well.
    assert 'id="stock-dialog"' not in html
    assert 'id="location-dialog"' not in html
    # The role is exposed so app.js can hide the table's Add/Take row buttons.
    assert 'name="user-role" content="read-only"' in html


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
        json={
            "type_id": ctype["id"],
            "manufacturer": "Yageo",
            "mpn": "RC0603",
            "notes": "Thick Film Resistors - SMD 10Kohms 1%",
        },
    )

    feed = client.get("/web/api/components").json()
    fields = [c["field"] for c in feed["columns"]]
    assert fields == [
        "type",
        "manufacturer",
        "mpn",
        "notes",
        "package",
        "mounting_type",
        "quantity",
    ]
    # Titled for what it holds — a shop import puts the product description here.
    assert [c["title"] for c in feed["columns"]][3] == "Description"
    assert len(feed["data"]) == 1
    assert feed["data"][0]["manufacturer"] == "Yageo"
    assert feed["data"][0]["notes"] == "Thick Film Resistors - SMD 10Kohms 1%"


def test_components_feed_sends_an_empty_string_for_a_missing_description(
    client: TestClient,
) -> None:
    """Never None: the client escapes and ellipsises it as text."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post("/api/components", json={"type_id": ctype["id"]})
    assert client.get("/web/api/components").json()["data"][0]["notes"] == ""


def test_components_feed_trims_a_long_description(client: TestClient) -> None:
    """`notes` is uncapped free text and this feed loads on every page view.

    Without a trim one component with a novel in it would be downloaded in full
    every time — including by the invoice line dialog, which only wants the MPN.
    """
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        "/api/components", json={"type_id": ctype["id"], "notes": "x" * 5000}
    )
    shipped = client.get("/web/api/components").json()["data"][0]["notes"]
    assert len(shipped) < 250
    assert shipped.endswith("…")


def test_component_detail_shows_the_full_description(client: TestClient) -> None:
    """The table trims; the detail page is where the whole text lives."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    long_text = "Thick Film Resistors " * 30
    component = client.post(
        "/api/components", json={"type_id": ctype["id"], "notes": long_text}
    ).json()
    html = client.get(f"/components/{component['id']}").text
    assert long_text.strip() in html
    # Labelled, matching the table column and both dialogs.
    assert "<th>Description</th>" in html


def test_component_dialogs_call_the_field_description(client: TestClient) -> None:
    """One field, one name — it was "Notes" in the dialogs and unnamed elsewhere.

    Scoped to the component forms: attachments, links and invoices also render a
    ``name="notes"`` input on these pages, and "Notes" is still the right word
    there, so a page-wide search would be testing the wrong field.
    """
    import re

    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    labelled = re.compile(r"<label>(\w+)</label>\s*<input[^>]*\bname=\"notes\"")

    pages = {
        "/": "component-form",  # the New Component dialog
        f"/components/{component['id']}": "component-edit-form",
    }
    for path, form_id in pages.items():
        html = client.get(path).text
        start = html.index(f'<form id="{form_id}"')
        form = html[start : html.index("</form>", start)]
        assert labelled.findall(form) == ["Description"]


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
    # A second component leaves the parameter unset.
    unset = client.post("/api/components", json={"type_id": ctype["id"]}).json()

    feed = client.get("/web/api/components", params={"type_id": ctype["id"]}).json()
    column = next(c for c in feed["columns"] if c["title"] == "Resistance")
    field = f"param_{definition['id']}"
    rows = {r["id"]: r for r in feed["data"]}
    # Engineering-formatted value with the definition's unit (decision D4)...
    assert rows[component["id"]][field] == "4.7 kohm"
    # ...plus a raw value + `numeric` flag so the client sorts by magnitude.
    assert column["numeric"] is True
    assert rows[component["id"]][f"{field}__n"] == 4700.0
    # An unset numeric value carries a None raw value (sorts to one end), not a
    # missing key.
    assert rows[unset["id"]][field] == ""
    assert rows[unset["id"]][f"{field}__n"] is None


def test_components_feed_text_param_column_is_not_numeric(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    client.post(
        f"/api/types/{ctype['id']}/parameters",
        json={
            "name": "tolerance",
            "label": "Tolerance",
            "data_type": "text",
            "is_table_column": True,
        },
    )
    definition = client.get(f"/api/types/{ctype['id']}/parameters").json()[0]
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    client.put(
        f"/api/components/{component['id']}/parameters",
        json={"parameter_definition_id": definition["id"], "value": "1%"},
    )

    feed = client.get("/web/api/components", params={"type_id": ctype["id"]}).json()
    column = next(c for c in feed["columns"] if c["title"] == "Tolerance")
    assert column["numeric"] is False
    # No numeric companion for a text column.
    assert f"param_{definition['id']}__n" not in feed["data"][0]


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
    # The attachments panel (§10) is present and wired to this component.
    html = response.text
    assert "attachments-widget" in html
    assert 'data-entity-type="component"' in html
    assert "attachments.js" in html
    assert "attachment-form" in html  # a writer sees the upload form
    # The header image gallery + lightbox (§10) and its script are present.
    assert 'id="component-images"' in html
    assert 'id="image-lightbox"' in html
    assert "image_gallery.js" in html


def test_component_detail_admin_sees_the_edit_dialog(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post(
        "/api/components", json={"type_id": ctype["id"], "mpn": "RC0603"}
    ).json()
    html = client.get(f"/components/{component['id']}").text  # client is admin
    assert 'id="component-edit-btn"' in html
    assert 'id="component-edit-dialog"' in html
    assert "component_edit.js" in html


def test_component_detail_non_admin_has_no_edit_affordance(
    client: TestClient, anon_client: TestClient
) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    token = _non_admin_token(client, role="user", username="editor")
    headers = {"Authorization": f"Bearer {token}"}
    html = anon_client.get(f"/components/{component['id']}", headers=headers).text
    # A plain writer can create components but not edit them — no button, no dialog.
    assert 'id="component-edit-btn"' not in html
    assert 'id="component-edit-dialog"' not in html
    assert "component_edit.js" not in html


def test_component_detail_writer_can_add_and_take_stock(client: TestClient) -> None:
    """The list's row actions, replicated on the detail page (same shared dialog)."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    html = client.get(f"/components/{component['id']}").text

    assert f'data-stock-act="add" data-component-id="{component["id"]}"' in html
    assert f'data-stock-act="take" data-component-id="{component["id"]}"' in html
    assert 'id="stock-dialog"' in html
    # The picker offers "+ New location" inline, so that dialog must be there too.
    assert 'id="location-dialog"' in html
    # The picker is populated: without `location_tree` in this route's context the
    # dialog would render "No locations yet" and be permanently unsubmittable, and
    # an id-only assertion would still pass.
    client.post("/api/locations", json={"name": "Drawer 5", "type": "drawer"})
    html = client.get(f"/components/{component['id']}").text
    assert 'class="loc-picker-node"' in html
    assert "Drawer 5" in html


def test_location_usage_feed_drives_the_stock_dialog_filter(
    client: TestClient,
) -> None:
    """Take offers `holding`; Add offers everything outside `occupied`, plus it."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    part = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    other = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    mine = client.post("/api/locations", json={"name": "D1", "type": "drawer"}).json()
    theirs = client.post("/api/locations", json={"name": "D2", "type": "drawer"}).json()
    free = client.post("/api/locations", json={"name": "D3", "type": "drawer"}).json()
    for component, location in ((part, mine), (other, theirs)):
        client.post(
            "/api/stock/add",
            json={
                "component_id": component["id"],
                "location_id": location["id"],
                "quantity": 4,
            },
        )

    usage = client.get(f"/web/api/components/{part['id']}/location-usage").json()
    assert usage["holding"] == [mine["id"]]
    assert sorted(usage["occupied"]) == sorted([mine["id"], theirs["id"]])
    # The free drawer is in neither set, so Add offers it and Take doesn't.
    assert free["id"] not in usage["occupied"]


def test_location_picker_announces_its_filter_notice(client: TestClient) -> None:
    """The notice appears/changes as the filter does, so it must be announced.

    Asserted on the rendered template rather than a jsdom fixture — a fixture
    would only prove the fixture carries the attribute.
    """
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    part = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    html = client.get(f"/components/{part['id']}").text
    assert 'class="loc-picker-nomatch" role="status" aria-live="polite"' in html
    assert 'class="loc-picker-showall"' in html


def test_location_usage_for_an_unknown_component_is_404(client: TestClient) -> None:
    assert client.get("/web/api/components/999/location-usage").status_code == 404


def test_location_usage_readable_by_read_only_but_not_anonymously(
    client: TestClient, anon_client: TestClient
) -> None:
    """It's a read of stock data — a viewer's picker may be filtered too."""
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    part = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    url = f"/web/api/components/{part['id']}/location-usage"

    token = _non_admin_token(client, role="read-only", username="viewer3")
    resp = anon_client.get(url, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    # Assert the BODY: require_web_user answers an unauthenticated request with a
    # 303 to /login, which the test client follows into a 200 — so a status-only
    # check here would pass with no auth at all.
    assert set(resp.json()) == {"holding", "occupied"}

    anonymous = anon_client.get(url, follow_redirects=False)
    assert anonymous.status_code == 303


def test_component_detail_read_only_cannot_move_stock(
    client: TestClient, anon_client: TestClient
) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    token = _non_admin_token(client, role="read-only", username="viewer2")
    headers = {"Authorization": f"Bearer {token}"}
    html = anon_client.get(f"/components/{component['id']}", headers=headers).text

    # Buttons AND dialog absent together — a hidden trigger isn't the boundary.
    assert "data-stock-act" not in html
    assert 'id="stock-dialog"' not in html


def test_component_detail_attachments_read_only_has_no_upload_form(
    client: TestClient, anon_client: TestClient
) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    token = _non_admin_token(client, role="read-only", username="viewer")
    headers = {"Authorization": f"Bearer {token}"}

    html = anon_client.get(f"/components/{component['id']}", headers=headers).text
    # Read-only still sees the panel (to view/download) but not the upload form.
    assert "attachments-widget" in html
    assert "attachment-form" not in html
    # The image gallery is view-only, so it renders for read-only accounts too.
    assert 'id="component-images"' in html
    assert "image_gallery.js" in html


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
    # The list is a Tabulator fed by JSON; the page renders the mount + loader.
    html = client.get("/invoices").text
    assert "Invoices" in html
    assert 'id="invoices-table"' in html
    assert "invoices_table.js" in html


def test_invoices_feed_orders_and_formats(client: TestClient) -> None:
    _invoice_with_line(client)  # draft INV-1 (Mouser), 2026-07-08
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
    feed = client.get("/web/api/invoices").json()
    numbers = [row["invoice_number"] for row in feed["data"]]
    assert numbers.index("INV-2") < numbers.index("INV-1")

    row = next(r for r in feed["data"] if r["invoice_number"] == "INV-2")
    assert "id" in row  # the client links each row to /invoices/<id>
    assert row["status"] == "draft"
    assert row["gross"] == "—"  # a draft has no gross yet
    assert row["net"].endswith("USD")  # money pre-formatted with its currency
    assert feed["truncated"] is False


def test_invoices_list_page_empty(client: TestClient) -> None:
    # The page renders even with no invoices; the (empty) feed drives the table.
    assert client.get("/invoices").status_code == 200
    assert client.get("/web/api/invoices").json()["data"] == []


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


def test_invoice_detail_shows_attachments_even_when_finalized(
    client: TestClient,
) -> None:
    invoice = _invoice_with_line(client)["invoice"]
    client.post(f"/api/invoices/{invoice['id']}/finalize", json={})  # type: ignore[index]

    html = client.get(f"/invoices/{invoice['id']}").text  # type: ignore[index]
    assert "attachments-widget" in html
    assert 'data-entity-type="invoice"' in html
    # Uploads are gated on writer role, not can_edit, so the form stays on a
    # finalized invoice (e.g. to attach the scanned PDF afterwards).
    assert "attachment-form" in html


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
    """When the list hits its cap, the feed flags it so the UI can say so
    instead of silently dropping rows."""
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
    feed = client.get("/web/api/invoices").json()
    assert feed["truncated"] is True
    assert feed["limit"] == 1
    assert len(feed["data"]) == 1


def test_invoice_pages_require_login(anon_client: TestClient) -> None:
    """The invoice pages and the list feed redirect an anonymous visitor."""
    for path in ("/invoices", "/invoices/1", "/web/api/invoices"):
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
    # The add-line picker can create a component inline via the shared dialog.
    assert 'id="invoice-add-component-btn"' in html
    assert 'id="component-dialog"' in html
    assert "/static/component_dialog.js" in html
    # …and the New Type builder is here too, so "+ New type" in that dialog works.
    assert 'id="type-dialog"' in html
    assert "/static/type_dialog.js" in html
    # The line's location field is the (optional) tree-picker.
    assert 'class="loc-picker"' in html
    assert 'data-loc-path="D1"' in html  # the seeded location
    assert "— none —" in html
    # …and it can create a location inline via the shared New Location dialog.
    assert 'class="loc-picker-new"' in html
    assert 'id="location-dialog"' in html


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
        'id="component-dialog"',
        'id="type-dialog"',
        'id="location-dialog"',
        'id="invoice-add-component-btn"',
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
    assert 'id="location-dialog"' not in html
    # The New component + New type builders are writer-gated too.
    assert 'id="component-dialog"' not in html
    assert 'id="type-dialog"' not in html


def test_locations_page_renders_tree(client: TestClient) -> None:
    """The Locations page shows the hierarchy and the create dialog (§7)."""
    room = client.post(
        "/api/locations", json={"type": "room", "name": "Lab"}
    ).json()
    rack = client.post(
        "/api/locations",
        json={"type": "rack", "name": "Rack A", "parent_id": room["id"]},
    ).json()
    # A third level so the recursive tree macro is actually exercised.
    client.post(
        "/api/locations",
        json={"type": "shelf", "name": "Shelf 1", "parent_id": rack["id"]},
    )
    html = client.get("/locations").text
    assert "Locations" in html
    assert "Lab" in html
    assert "Rack A" in html
    assert "Shelf 1" in html
    # Writer controls and the shared dialog are wired up.
    assert 'id="new-location-btn"' in html
    assert 'id="location-dialog"' in html
    assert "/static/location_dialog.js" in html
    # The parent picker lists existing locations by their full (nested) path.
    assert "Lab / Rack A / Shelf 1" in html
    # The nav links to the page.
    assert 'href="/locations"' in html


def test_locations_page_empty(client: TestClient) -> None:
    assert "No locations yet." in client.get("/locations").text


def test_location_name_is_html_escaped(client: TestClient) -> None:
    """A location name with HTML metacharacters renders escaped (no injection)."""
    client.post(
        "/api/locations", json={"type": "box", "name": 'Evil"><script>x</script>'}
    )
    html = client.get("/locations").text
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_new_location_control_hidden_for_read_only(client: TestClient) -> None:
    html = client.get("/locations", headers=_read_only_headers(client)).text
    assert 'id="new-location-btn"' not in html
    # The dialog markup itself is gated too, not just the button.
    assert 'id="location-dialog"' not in html
    # The nav entry is still there (browsing is allowed).
    assert 'href="/locations"' in html


def test_locations_page_survives_a_cyclic_hierarchy(
    client: TestClient,
    session,  # type: ignore[no-untyped-def]
) -> None:
    """A cycle (unreachable via the API) must not hang the render (§7)."""
    from app.models.location import Location

    a = client.post("/api/locations", json={"type": "room", "name": "A"}).json()
    b = client.post(
        "/api/locations",
        json={"type": "room", "name": "B", "parent_id": a["id"]},
    ).json()
    # Force A -> B -> A directly, bypassing create_location's guards.
    loc_a = session.get(Location, a["id"])
    loc_a.parent_id = b["id"]
    session.add(loc_a)
    session.commit()

    # path_of's visited-set guard keeps the walk finite.
    assert client.get("/locations").status_code == 200


def test_locations_page_requires_login(anon_client: TestClient) -> None:
    resp = anon_client.get("/locations", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def _non_admin_token(
    client: TestClient, role: str = "user", username: str = "bob"
) -> str:
    """Create a non-admin account (via the admin client) and return its token."""
    client.post(
        "/api/admin/users",
        json={"username": username, "password": "password123", "role": role},
    )
    return client.post(
        "/api/auth/token", json={"username": username, "password": "password123"}
    ).json()["access_token"]


def test_users_page_renders_for_admin(client: TestClient) -> None:
    # The list is a Tabulator fed by JSON; the page renders the mount + loader.
    html = client.get("/users").text
    assert "Users" in html
    assert 'id="users-table"' in html
    assert "users.js" in html
    # The create form defaults to the least-privileged role, never admin.
    assert '<option value="user" selected>' in html


def test_users_feed_lists_accounts(client: TestClient) -> None:
    _non_admin_token(client, role="read-only")  # a second account to list
    resp = client.get("/web/api/users")
    # Sensitive account data must not be cached (shared stations / bfcache).
    assert resp.headers["cache-control"] == "no-store"
    feed = resp.json()
    names = {row["name"]: row for row in feed["data"]}
    assert names["admin"]["role"] == "admin"
    assert names["admin"]["is_active"] is True
    assert names["bob"]["role"] == "read-only"
    # The feed must never expose the password hash (regression guard).
    assert all("password_hash" not in row for row in feed["data"])


def test_users_page_forbidden_for_non_admin(client: TestClient) -> None:
    # Both non-admin roles are sent home, not to login, on the page and the feed.
    for role in ("user", "read-only"):
        token = _non_admin_token(client, role=role, username=f"acct_{role}")
        headers = {"Authorization": f"Bearer {token}"}
        for path in ("/users", "/web/api/users"):
            resp = client.get(path, headers=headers, follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/"


def test_users_pages_require_login(anon_client: TestClient) -> None:
    for path in ("/users", "/web/api/users"):
        resp = anon_client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


_BOM_CSV = (Path(__file__).parent / "fixtures" / "kicad_bom.csv").read_bytes()


def _upload_bom(client: TestClient, tmp_path, monkeypatch) -> int:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    resp = client.post(
        "/api/boms",
        files={"file": ("kicad_bom.csv", _BOM_CSV, "text/csv")},
        data={"name": "hiduart"},
    )
    return resp.json()["id"]


def test_boms_page_renders_with_upload_for_writer(client: TestClient) -> None:
    html = client.get("/boms").text
    assert "BOMs" in html
    assert 'id="bom-upload-btn"' in html
    assert 'id="bom-upload-form"' in html
    assert "boms.js" in html


def test_boms_page_has_no_upload_for_read_only(
    client: TestClient, anon_client: TestClient
) -> None:
    token = _non_admin_token(client, role="read-only", username="viewer")
    html = anon_client.get(
        "/boms", headers={"Authorization": f"Bearer {token}"}
    ).text
    assert "BOMs" in html
    assert 'id="bom-upload-btn"' not in html
    assert 'id="bom-upload-form"' not in html


def test_bom_report_page_renders(
    client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bom_id = _upload_bom(client, tmp_path, monkeypatch)
    html = client.get(f"/boms/{bom_id}").text
    # A shell page: the header + the Tabulator mount + the feed hook. The lines and
    # summary are fetched client-side from /api/boms/{id}/report (see the JS tests).
    assert "hiduart" in html  # the BOM name in the header
    assert 'id="bom-lines-table"' in html
    assert f'data-bom-id="{bom_id}"' in html
    assert "boms_report.js" in html
    # The original CSV is downloadable.
    assert "/api/attachments/" in html and "/download" in html


def test_bom_report_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/boms/9999").status_code == 404


def test_bom_report_escapes_bom_name(
    client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    bom_id = client.post(
        "/api/boms",
        files={"file": ("kicad_bom.csv", _BOM_CSV, "text/csv")},
        data={"name": "<b>pwn</b>"},
    ).json()["id"]

    html = client.get(f"/boms/{bom_id}").text
    # The name is the only untrusted field rendered server-side (header + title);
    # Jinja autoescape neutralises it. CSV line content is rendered client-side, and
    # its escaping is covered in tests/js/boms_report.test.js.
    assert "<b>pwn</b>" not in html
    assert "&lt;b&gt;pwn&lt;/b&gt;" in html


def test_bom_report_renders_without_a_csv_attachment(
    client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bom_id = _upload_bom(client, tmp_path, monkeypatch)
    stored = client.get(
        "/api/attachments", params={"entity_type": "bom", "entity_id": bom_id}
    ).json()
    client.delete(f"/api/attachments/{stored[0]['id']}")  # remove the CSV

    resp = client.get(f"/boms/{bom_id}")
    assert resp.status_code == 200
    assert "Download CSV" not in resp.text


def test_bom_report_includes_add_component_dialog_for_writer(
    client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bom_id = _upload_bom(client, tmp_path, monkeypatch)
    html = client.get(f"/boms/{bom_id}").text
    # The shared New Component dialog is present so a missing line can be added.
    assert 'id="component-dialog"' in html
    assert 'id="component-form"' in html


def test_bom_report_has_no_add_component_dialog_for_read_only(
    client: TestClient, anon_client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bom_id = _upload_bom(client, tmp_path, monkeypatch)
    token = _non_admin_token(client, role="read-only", username="viewer_bom")
    html = anon_client.get(
        f"/boms/{bom_id}", headers={"Authorization": f"Bearer {token}"}
    ).text
    assert 'id="component-dialog"' not in html

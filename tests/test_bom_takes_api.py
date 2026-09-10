"""API tests for taking a BOM off the shelves: preview, run, read, undo."""

from __future__ import annotations

import pytest
from app import config
from fastapi.testclient import TestClient

_FIXTURE = b"Reference,Qty,Value,MPN\nU1,2,x,PART-A\n"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


def _held(client: TestClient, take_ready) -> int:  # type: ignore[no-untyped-def]
    """What the gathering drawer holds right now."""
    return client.get(
        "/api/stock/quantity",
        params={
            "component_id": take_ready["component_id"],
            "location_id": take_ready["drawer_id"],
        },
    ).json()["quantity"]


@pytest.fixture
def take_ready(client: TestClient):  # type: ignore[no-untyped-def]
    """A one-line BOM, assigned, with 100 in a drawer under a gathering box."""
    gathering = client.post(
        "/api/locations", json={"type": "box", "name": "Kontroler CNC"}
    ).json()
    drawer = client.post(
        "/api/locations",
        json={"type": "drawer", "name": "Rezystory", "parent_id": gathering["id"]},
    ).json()
    ctype = client.post("/api/types", json={"name": "IC"}).json()
    component = client.post(
        "/api/components", json={"name": "A", "type_id": ctype["id"], "mpn": "PART-A"}
    ).json()
    client.post(
        "/api/stock/add",
        json={
            "component_id": component["id"],
            "location_id": drawer["id"],
            "quantity": 100,
        },
    )
    bom = client.post(
        "/api/boms",
        files={"file": ("b.csv", _FIXTURE, "text/csv")},
        data={"name": "Kontroler CNC"},
    ).json()
    client.post(f"/api/boms/{bom['id']}/assign-obvious")
    return {
        "bom_id": bom["id"],
        "gathering_id": gathering["id"],
        "drawer_id": drawer["id"],
        "component_id": component["id"],
    }


def test_preview_says_what_would_happen_without_doing_it(
    client: TestClient, take_ready
) -> None:  # type: ignore[no-untyped-def]
    resp = client.post(
        f"/api/boms/{take_ready['bom_id']}/take/preview",
        json={"boards": 3, "source_location_id": take_ready["gathering_id"]},
    )

    assert resp.status_code == 200
    plan = resp.json()
    assert plan["can_run"] is True and plan["boards"] == 3
    line = plan["lines"][0]
    assert line["requested"] == 6  # 2 per board × 3
    assert [(s["location_id"], s["quantity"]) for s in line["sources"]] == [
        (take_ready["drawer_id"], 6)
    ]
    assert _held(client, take_ready) == 100  # nothing moved


def test_a_take_removes_the_stock_and_answers_with_its_snapshot(
    client: TestClient, take_ready
) -> None:  # type: ignore[no-untyped-def]
    resp = client.post(
        f"/api/boms/{take_ready['bom_id']}/takes",
        json={"boards": 3, "source_location_id": take_ready["gathering_id"]},
    )

    assert resp.status_code == 201
    take = resp.json()
    assert take["name"].startswith("Kontroler CNC ")
    assert _held(client, take_ready) == 94

    detail = client.get(f"/api/bom-takes/{take['id']}").json()
    assert detail["lines"][0]["taken"] == 6
    assert detail["lines"][0]["sources"][0]["path"].endswith("Rezystory")
    past = client.get(f"/api/boms/{take_ready['bom_id']}/takes").json()
    assert [t["id"] for t in past] == [take["id"]]


def test_a_typed_quantity_is_honoured(client: TestClient, take_ready) -> None:  # type: ignore[no-untyped-def]
    line_id = client.get(f"/api/boms/{take_ready['bom_id']}").json()["lines"][0]["id"]

    resp = client.post(
        f"/api/boms/{take_ready['bom_id']}/takes",
        json={
            "boards": 3,
            "source_location_id": take_ready["gathering_id"],
            "lines": [{"line_id": line_id, "quantity": 10}],
        },
    )

    assert resp.status_code == 201
    detail = client.get(f"/api/bom-takes/{resp.json()['id']}").json()
    assert detail["lines"][0]["requested"] == 10


def test_two_places_outside_ask_first_and_then_honour_the_answer(
    client: TestClient, take_ready
) -> None:  # type: ignore[no-untyped-def]
    """The one question the take asks — and the answer has to reach the plan."""
    shelf_a = client.post(
        "/api/locations", json={"type": "shelf", "name": "Regal A"}
    ).json()
    shelf_b = client.post(
        "/api/locations", json={"type": "shelf", "name": "Regal B"}
    ).json()
    for shelf in (shelf_a, shelf_b):
        client.post(
            "/api/stock/add",
            json={
                "component_id": take_ready["component_id"],
                "location_id": shelf["id"],
                "quantity": 50,
            },
        )
    # Empty the gathering drawer so the fallback is what decides.
    client.post(
        "/api/stock/remove",
        json={
            "component_id": take_ready["component_id"],
            "location_id": take_ready["drawer_id"],
            "quantity": 100,
        },
    )
    body = {"boards": 1, "source_location_id": take_ready["gathering_id"]}
    line_id = client.get(f"/api/boms/{take_ready['bom_id']}").json()["lines"][0]["id"]

    plan = client.post(
        f"/api/boms/{take_ready['bom_id']}/take/preview", json=body
    ).json()
    assert plan["lines"][0]["needs_choice"] is True
    assert plan["can_run"] is False
    assert {c["location_id"] for c in plan["lines"][0]["candidates"]} == {
        shelf_a["id"],
        shelf_b["id"],
    }
    assert (
        client.post(f"/api/boms/{take_ready['bom_id']}/takes", json=body).status_code
        == 422
    )

    answered = {
        **body,
        "lines": [{"line_id": line_id, "source_location_id": shelf_b["id"]}],
    }
    resp = client.post(f"/api/boms/{take_ready['bom_id']}/takes", json=answered)

    assert resp.status_code == 201
    detail = client.get(f"/api/bom-takes/{resp.json()['id']}").json()
    assert [s["location_id"] for s in detail["lines"][0]["sources"]] == [shelf_b["id"]]


def test_an_unresolved_line_is_refused_by_name(client: TestClient, take_ready) -> None:  # type: ignore[no-untyped-def]
    """The message has to name the lines, or there is nothing to act on."""
    unresolved = b"Reference,Qty,Value,MPN\nU9,1,x,NOBODY\n"
    bom = client.post(
        "/api/boms",
        files={"file": ("b.csv", unresolved, "text/csv")},
        data={"name": "Other"},
    ).json()

    resp = client.post(
        f"/api/boms/{bom['id']}/takes",
        json={"boards": 1, "source_location_id": take_ready["gathering_id"]},
    )

    assert resp.status_code == 422
    assert "U9" in resp.json()["detail"]


def test_reversing_puts_it_back_and_refuses_a_second_time(
    client: TestClient, take_ready
) -> None:  # type: ignore[no-untyped-def]
    take = client.post(
        f"/api/boms/{take_ready['bom_id']}/takes",
        json={"boards": 3, "source_location_id": take_ready["gathering_id"]},
    ).json()

    resp = client.post(
        f"/api/bom-takes/{take['id']}/reverse", json={"reason": "board scrapped"}
    )

    assert resp.status_code == 200
    assert resp.json()["reversal_reason"] == "board scrapped"
    assert _held(client, take_ready) == 100
    again = client.post(
        f"/api/bom-takes/{take['id']}/reverse", json={"reason": "again"}
    )
    assert again.status_code == 422


def test_a_reversal_needs_a_reason(client: TestClient, take_ready) -> None:  # type: ignore[no-untyped-def]
    take = client.post(
        f"/api/boms/{take_ready['bom_id']}/takes",
        json={"boards": 1, "source_location_id": take_ready["gathering_id"]},
    ).json()

    assert (
        client.post(
            f"/api/bom-takes/{take['id']}/reverse", json={"reason": "  "}
        ).status_code
        == 422
    )


def test_unknown_ids_are_404(client: TestClient, take_ready) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/bom-takes/9999").status_code == 404
    assert (
        client.post("/api/bom-takes/9999/reverse", json={"reason": "x"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/boms/9999/take/preview",
            json={"boards": 1, "source_location_id": take_ready["gathering_id"]},
        ).status_code
        == 404
    )


def test_read_only_can_look_but_not_take(
    client: TestClient, anon_client: TestClient, take_ready
) -> None:  # type: ignore[no-untyped-def]
    take = client.post(
        f"/api/boms/{take_ready['bom_id']}/takes",
        json={"boards": 1, "source_location_id": take_ready["gathering_id"]},
    ).json()
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    body = {"boards": 1, "source_location_id": take_ready["gathering_id"]}

    assert (
        anon_client.post(
            f"/api/boms/{take_ready['bom_id']}/takes", json=body, headers=headers
        ).status_code
        == 403
    )
    assert (
        anon_client.post(
            f"/api/boms/{take_ready['bom_id']}/take/preview", json=body, headers=headers
        ).status_code
        == 403
    )
    assert (
        anon_client.post(
            f"/api/bom-takes/{take['id']}/reverse",
            json={"reason": "x"},
            headers=headers,
        ).status_code
        == 403
    )
    # …but the record itself is readable.
    assert (
        anon_client.get(f"/api/bom-takes/{take['id']}", headers=headers).status_code
        == 200
    )

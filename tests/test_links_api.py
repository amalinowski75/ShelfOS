"""API-level tests for external links."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _component_id(client: TestClient) -> int:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    return client.post("/api/components", json={"type_id": ctype["id"]}).json()["id"]


def _account_headers(
    client: TestClient, anon_client: TestClient, *, role: str, username: str
) -> dict[str, str]:
    client.post(
        "/api/admin/users",
        json={"username": username, "password": "password123", "role": role},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": username, "password": "password123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_create_returns_the_link_with_its_url(client: TestClient) -> None:
    cid = _component_id(client)
    resp = client.post(
        "/api/links",
        json={
            "entity_type": "component",
            "entity_id": cid,
            "kind": "shop",
            "url": "https://www.tme.eu/pl/details/x/y/",
            "label": "TME",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # Unlike attachments, the URL is part of the read model — it is the link.
    assert body["url"] == "https://www.tme.eu/pl/details/x/y/"
    assert body["kind"] == "shop"
    assert body["label"] == "TME"


def test_create_rejects_a_non_web_scheme(client: TestClient) -> None:
    cid = _component_id(client)
    resp = client.post(
        "/api/links",
        json={
            "entity_type": "component",
            "entity_id": cid,
            "url": "javascript:alert(1)",
        },
    )
    assert resp.status_code == 422  # ValidationError → 422


def test_list_returns_links_oldest_first(client: TestClient) -> None:
    cid = _component_id(client)
    for url in ("https://a.io", "https://b.io"):
        client.post(
            "/api/links",
            json={"entity_type": "component", "entity_id": cid, "url": url},
        )
    resp = client.get(
        "/api/links", params={"entity_type": "component", "entity_id": cid}
    )
    assert resp.status_code == 200
    assert [row["url"] for row in resp.json()] == ["https://a.io", "https://b.io"]


def test_delete_removes_a_link(client: TestClient) -> None:
    cid = _component_id(client)
    link_id = client.post(
        "/api/links",
        json={"entity_type": "component", "entity_id": cid, "url": "https://x.io"},
    ).json()["id"]
    assert client.delete(f"/api/links/{link_id}").status_code == 204
    listed = client.get(
        "/api/links", params={"entity_type": "component", "entity_id": cid}
    ).json()
    assert listed == []


def _make_link(client: TestClient, cid: int) -> dict[str, object]:
    return client.post(
        "/api/links",
        json={
            "entity_type": "component",
            "entity_id": cid,
            "kind": "shop",
            "url": "https://old.example/x",
            "label": "Old",
            "notes": "old",
        },
    ).json()


def test_update_replaces_the_editable_fields(client: TestClient) -> None:
    cid = _component_id(client)
    link = _make_link(client, cid)
    resp = client.patch(
        f"/api/links/{link['id']}",
        json={
            "kind": "datasheet",
            "url": "https://new.example/y",
            "label": "New",
            "notes": "new",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == link["id"]
    assert body["kind"] == "datasheet"
    assert body["url"] == "https://new.example/y"
    assert body["label"] == "New"
    assert body["notes"] == "new"


def test_update_blank_label_and_notes_clears_them(client: TestClient) -> None:
    cid = _component_id(client)
    link = _make_link(client, cid)
    # A full replace: omitting label/notes clears the previously stored values.
    resp = client.patch(
        f"/api/links/{link['id']}",
        json={"kind": "shop", "url": "https://x.io"},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] is None
    assert resp.json()["notes"] is None


def test_update_rejects_a_non_web_scheme(client: TestClient) -> None:
    cid = _component_id(client)
    link = _make_link(client, cid)
    resp = client.patch(
        f"/api/links/{link['id']}",
        json={"kind": "shop", "url": "javascript:alert(1)"},
    )
    assert resp.status_code == 422  # ValidationError → 422


def test_update_of_a_missing_link_is_404(client: TestClient) -> None:
    resp = client.patch("/api/links/9999", json={"url": "https://x.io"})
    assert resp.status_code == 404  # NotFoundError → 404


def test_create_on_a_missing_entity_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/links",
        json={"entity_type": "component", "entity_id": 9999, "url": "https://x.io"},
    )
    assert resp.status_code == 404  # NotFoundError → 404


def test_create_with_an_unknown_entity_type_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/links",
        json={"entity_type": "widget", "entity_id": 1, "url": "https://x.io"},
    )
    assert resp.status_code == 422  # ValidationError → 422


def test_delete_of_a_missing_link_is_404(client: TestClient) -> None:
    assert client.delete("/api/links/9999").status_code == 404


def test_read_only_can_list_but_not_create(
    client: TestClient, anon_client: TestClient
) -> None:
    cid = _component_id(client)
    headers = _account_headers(
        client, anon_client, role="read-only", username="viewer"
    )
    # GET is allowed…
    assert (
        anon_client.get(
            "/api/links",
            params={"entity_type": "component", "entity_id": cid},
            headers=headers,
        ).status_code
        == 200
    )
    # …POST is not.
    assert (
        anon_client.post(
            "/api/links",
            json={"entity_type": "component", "entity_id": cid, "url": "https://x.io"},
            headers=headers,
        ).status_code
        == 403
    )
    # …and neither is PATCH (editing an existing link).
    link_id = client.post(
        "/api/links",
        json={"entity_type": "component", "entity_id": cid, "url": "https://x.io"},
    ).json()["id"]
    assert (
        anon_client.patch(
            f"/api/links/{link_id}",
            json={"url": "https://y.io"},
            headers=headers,
        ).status_code
        == 403
    )

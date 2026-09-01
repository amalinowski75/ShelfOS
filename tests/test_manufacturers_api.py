"""API for "you may already have this part" and the answer to it."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _component(client: TestClient, *, mpn: str, manufacturer: str) -> int:
    type_id = client.post("/api/types", json={"name": f"t-{mpn}"}).json()["id"]
    resp = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": mpn, "manufacturer": manufacturer},
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def test_it_lists_the_part_under_its_other_name(client: TestClient) -> None:
    existing = _component(client, mpn="MCP2200", manufacturer="Microchip Technology")
    resp = client.get(
        "/api/manufacturers/same-mpn",
        params={"mpn": "MCP2200", "manufacturer": "MICROCHIP"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body["candidates"]] == [existing]
    candidate = body["candidates"][0]
    # Enough to recognise the part by: the maker is the comparison, the type and
    # description are how you tell two same-MPN parts apart.
    assert candidate["manufacturer"] == "Microchip Technology"
    assert candidate["type_name"] == "t-MCP2200"
    # The incoming name echoed back as it resolves TODAY — no alias yet, so as sent.
    assert body["manufacturer"] == "MICROCHIP"


def test_an_exact_match_is_listed_too(client: TestClient) -> None:
    # Not an ambiguity but a duplicate, and the user is better told now than at
    # "Create component" — which is where they used to find out.
    existing = _component(client, mpn="MCP2200", manufacturer="Microchip Technology")
    resp = client.get(
        "/api/manufacturers/same-mpn",
        params={"mpn": "MCP2200", "manufacturer": "Microchip Technology"},
    )
    assert [c["id"] for c in resp.json()["candidates"]] == [existing]


def test_recording_an_alias_resolves_the_name_from_then_on(client: TestClient) -> None:
    _component(client, mpn="MCP2200", manufacturer="Microchip Technology")
    created = client.post(
        "/api/manufacturers/aliases",
        json={"alias": "MICROCHIP", "canonical": "Microchip Technology"},
    )
    assert created.status_code == 200
    assert created.json()["canonical"] == "Microchip Technology"

    resp = client.get(
        "/api/manufacturers/same-mpn",
        params={"mpn": "MCP2200", "manufacturer": "MICROCHIP"},
    )
    body = resp.json()
    # The spelling now means the stored maker — which is the alias's whole job.
    assert body["manufacturer"] == "Microchip Technology"
    # The part is still listed: the user owns it, and that is worth saying whether
    # or not the naming was ambiguous.
    assert len(body["candidates"]) == 1


def test_a_part_created_after_the_alias_lands_on_the_canonical_name(
    client: TestClient,
) -> None:
    client.post(
        "/api/manufacturers/aliases",
        json={"alias": "MICROCHIP", "canonical": "Microchip Technology"},
    )
    type_id = client.post("/api/types", json={"name": "ic"}).json()["id"]
    created = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "MCP2221", "manufacturer": "MICROCHIP"},
    )
    assert created.json()["manufacturer"] == "Microchip Technology"


def test_creating_the_same_part_under_an_alias_is_refused_as_a_duplicate(
    client: TestClient,
) -> None:
    # The point of the whole feature: once the user has said the two names are one
    # maker, the second spelling stops making a second component.
    existing = _component(client, mpn="MCP2200", manufacturer="Microchip Technology")
    client.post(
        "/api/manufacturers/aliases",
        json={"alias": "MICROCHIP", "canonical": "Microchip Technology"},
    )
    type_id = client.post("/api/types", json={"name": "ic2"}).json()["id"]
    resp = client.post(
        "/api/components",
        json={"type_id": type_id, "mpn": "MCP2200", "manufacturer": "MICROCHIP"},
    )
    assert resp.status_code == 409
    assert resp.json()["existing_id"] == existing


def test_nothing_to_record_answers_null_not_an_error(client: TestClient) -> None:
    # A blank incoming name (a Farnell invoice prints none) is an ordinary case,
    # and the client treats "recorded" and "nothing to record" the same way.
    resp = client.post(
        "/api/manufacturers/aliases", json={"alias": "", "canonical": "ONSEMI"}
    )
    assert resp.status_code == 200
    assert resp.json() is None


def test_an_alias_needs_something_to_point_at(client: TestClient) -> None:
    resp = client.post(
        "/api/manufacturers/aliases", json={"alias": "ONSEMI", "canonical": ""}
    )
    assert resp.status_code == 422


def test_both_endpoints_are_closed_to_read_only_accounts(
    client: TestClient, anon_client: TestClient
) -> None:
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # The lookup is a GET and decides nothing, so a reader may ask it…
    assert (
        anon_client.get(
            "/api/manufacturers/same-mpn",
            params={"mpn": "X"},
            headers=headers,
        ).status_code
        == 200
    )
    # …but recording an alias changes matching for everyone.
    assert (
        anon_client.post(
            "/api/manufacturers/aliases",
            json={"alias": "A", "canonical": "B"},
            headers=headers,
        ).status_code
        == 403
    )

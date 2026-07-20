"""API tests for the shop lookup endpoint (create a component from a shop URL)."""

from __future__ import annotations

from app.services import shops
from app.services.shops.base import ProductData
from fastapi.testclient import TestClient


def test_registry_dispatches_each_shop_by_host() -> None:
    """Registering a provider is the whole integration surface — check it took."""
    resolved = {
        url: getattr(shops.resolve(url), "name", None)
        for url in (
            "https://www.mouser.pl/pl/ProductDetail/Walsin/MR04X1201FTL",
            "https://www.digikey.pl/pl/products/detail/walsin/MR04X1201FTL/13908146",
            "https://www.tme.eu/pl/details/mr04x1201ftl/rezystory-smd-0402/walsin/",
            "https://example.com/part/1",
        )
    }
    assert list(resolved.values()) == ["Mouser", "Digi-Key", "TME", None]


def _read_only_headers(
    client: TestClient, anon_client: TestClient
) -> dict[str, str]:
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_lookup_returns_a_normalised_product(
    client: TestClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        shops,
        "lookup",
        lambda url: ProductData(
            mpn="MPN-1",
            manufacturer="ACME",
            description="desc",
            category="resistor",
            datasheet_url="https://x/d.pdf",
            parameters=[("Resistance", "10k")],
        ),
    )
    resp = client.post("/api/shops/lookup", json={"url": "https://www.mouser.com/x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mpn"] == "MPN-1"
    assert body["category"] == "resistor"
    assert body["datasheet_url"] == "https://x/d.pdf"
    assert body["parameters"] == [{"name": "Resistance", "value": "10k"}]


def test_lookup_unsupported_shop_is_422(client: TestClient) -> None:
    resp = client.post("/api/shops/lookup", json={"url": "https://www.example.com/x"})
    assert resp.status_code == 422  # no provider matches → ValidationError


def test_lookup_forbidden_for_read_only(
    client: TestClient, anon_client: TestClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(shops, "lookup", lambda url: ProductData(mpn="X"))
    headers = _read_only_headers(client, anon_client)
    resp = anon_client.post(
        "/api/shops/lookup", json={"url": "https://www.mouser.com/x"}, headers=headers
    )
    assert resp.status_code == 403

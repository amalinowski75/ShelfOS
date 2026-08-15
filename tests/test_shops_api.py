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


def _read_only_headers(client: TestClient, anon_client: TestClient) -> dict[str, str]:
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
        "import_code",
        lambda code: ProductData(
            mpn="MPN-1",
            manufacturer="ACME",
            description="desc",
            category="resistor",
            shop_category="Chip Resistor - Surface Mount",
            datasheet_url="https://x/d.pdf",
            parameters=[("Resistance", "10k")],
        ),
    )
    resp = client.post("/api/shops/lookup", json={"code": "https://www.mouser.com/x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mpn"] == "MPN-1"
    assert body["category"] == "resistor"
    # The raw shop category rides along; the dialog mines it for the mounting type.
    assert body["shop_category"] == "Chip Resistor - Surface Mount"
    assert body["datasheet_url"] == "https://x/d.pdf"
    assert body["parameters"] == [{"name": "Resistance", "value": "10k"}]


def test_lookup_unsupported_shop_is_422(client: TestClient) -> None:
    resp = client.post("/api/shops/lookup", json={"code": "https://www.example.com/x"})
    assert resp.status_code == 422  # no provider matches → ValidationError


def test_lookup_accepts_a_scanned_code(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The same endpoint takes a scanned barcode payload, not just a URL."""
    seen: dict[str, str] = {}

    def _import(code: str) -> ProductData:
        seen["code"] = code
        return ProductData(mpn="MIC334", manufacturer="Microchip")

    monkeypatch.setattr(shops, "import_code", _import)
    resp = client.post(
        "/api/shops/lookup",
        json={"code": "QTY:5 PN:MIC334 https://www.tme.eu/details/MIC334"},
    )
    assert resp.status_code == 200
    assert resp.json()["mpn"] == "MIC334"
    assert seen["code"].startswith("QTY:5")


def test_lookup_reports_a_scanner_that_drops_the_separators(
    client: TestClient,
) -> None:
    """A concatenated DataMatrix is refused with an explanation, not guessed at."""
    resp = client.post("/api/shops/lookup", json={"code": "[)>061P5277Q251VKeystone"})
    assert resp.status_code == 422
    assert "separator" in resp.text


def test_lookup_forbidden_for_read_only(
    client: TestClient, anon_client: TestClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(shops, "import_code", lambda code: ProductData(mpn="X"))
    headers = _read_only_headers(client, anon_client)
    resp = anon_client.post(
        "/api/shops/lookup", json={"code": "https://www.mouser.com/x"}, headers=headers
    )
    assert resp.status_code == 403


# --- /api/shops/parse: decode a label with no shop API call -----------------


def test_parse_decodes_a_datamatrix_offline(client: TestClient) -> None:
    """The identifiers come back as-is — no provider, no API key, no lookup."""
    label = "\x1d".join(
        ["[)>\x1e06", "1PESQ-106-33-T-S", "30PSAM11086-ND", "1VSamtec", "Q20"]
    )
    resp = client.post("/api/shops/parse", json={"code": label})
    assert resp.status_code == 200
    assert resp.json() == {
        "mpn": "ESQ-106-33-T-S",
        "manufacturer": "Samtec",
        "distributor_pn": "SAM11086-ND",
        "manufacturer_pn": None,
        "shop": "digikey",
        "url": None,
        "url_symbols": [],
    }


def test_parse_returns_a_tme_qr_url_and_pn(client: TestClient) -> None:
    resp = client.post(
        "/api/shops/parse",
        json={"code": "QTY:5 PN:MIC334 https://www.tme.eu/details/MIC334"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://www.tme.eu/details/MIC334"
    assert data["mpn"] == "MIC334"


def test_parse_reads_the_symbol_from_a_url_only_tme_qr(client: TestClient) -> None:
    """A QR carrying just the product URL is still matchable.

    Without the ``PN:`` token there is no mpn and no distributor_pn, so the
    path's symbol candidates are all a caller (scan putaway) can match a bag
    by. The position of the symbol in the path is not fixed, hence a list.
    """
    resp = client.post(
        "/api/shops/parse",
        json={"code": "https://www.tme.eu/pl/details/zl262-40dg/zlacza/connfly/"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mpn"] is None and data["distributor_pn"] is None
    assert "ZL262-40DG" in data["url_symbols"]
    # Over-long prose segments are not offered as symbols.
    assert all(len(s) <= 18 for s in data["url_symbols"])


def test_parse_reports_no_symbols_for_a_non_tme_url(client: TestClient) -> None:
    resp = client.post(
        "/api/shops/parse",
        json={"code": "https://www.mouser.pl/pl/ProductDetail/Walsin/MR04X1201FTL"},
    )
    assert resp.status_code == 200
    assert resp.json()["url_symbols"] == []


def test_parse_rejects_an_unreadable_code(client: TestClient) -> None:
    resp = client.post("/api/shops/parse", json={"code": "not a label"})
    assert resp.status_code == 422


def test_parse_forbidden_for_read_only(
    client: TestClient, anon_client: TestClient
) -> None:
    headers = _read_only_headers(client, anon_client)
    resp = anon_client.post(
        "/api/shops/parse", json={"code": "PN:MIC334"}, headers=headers
    )
    assert resp.status_code == 403

"""Tests for the Digi-Key provider (create a component from a shop URL)."""

from __future__ import annotations

import httpx
import pytest
from app import config
from app.services.errors import ValidationError
from app.services.shops import digikey
from app.services.shops.digikey import DigiKeyProvider

_PRODUCT = {
    "Product": {
        "ManufacturerProductNumber": "MR04X1201FTL",
        "Manufacturer": {"Name": "Walsin Technology Corporation"},
        "Description": {"ProductDescription": "RES SMD 1.2K OHM 1% 1/16W 0402"},
        "DatasheetUrl": "https://example.com/ds.pdf",
        "Category": {"Name": "Chip Resistor - Surface Mount"},
        "Parameters": [
            {"ParameterText": "Resistance", "ValueText": "1.2 kOhms"},
            {"ParameterText": "Tolerance", "ValueText": "±1%"},
        ],
    }
}


@pytest.fixture(autouse=True)
def _creds_and_fresh_token(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setattr(config, "DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(digikey, "_token_cache", None)  # never reuse across tests


def _transport(product: object = _PRODUCT, *, token_status: int = 200):
    """Routes the OAuth token POST and the product GET."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            if token_status >= 400:
                return httpx.Response(token_status, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        return httpx.Response(200, json=product)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.digikey.com/en/products/detail/walsin/MR04X1201FTL/13908146",
        "https://www.digikey.pl/pl/products/detail/walsin/MR04X1201FTL/13908146",
        "https://www.digikey.co.uk/x",
    ],
)
def test_matches_digikey_hosts(url: str) -> None:
    assert DigiKeyProvider().matches(url)


def test_does_not_match_another_shop() -> None:
    assert not DigiKeyProvider().matches("https://www.mouser.com/x")


def test_matches_is_false_for_a_malformed_url() -> None:
    assert DigiKeyProvider().matches("http://[::1/x") is False


def test_fetch_normalises_a_product() -> None:
    product = DigiKeyProvider().fetch(
        "https://www.digikey.pl/pl/products/detail/walsin/MR04X1201FTL/13908146",
        transport=_transport(),
    )
    assert product.mpn == "MR04X1201FTL"
    assert product.manufacturer == "Walsin Technology Corporation"
    assert product.datasheet_url == "https://example.com/ds.pdf"
    assert product.category == "resistor"  # inferred from "Chip Resistor…"
    # Values stay RAW; cleaning is client-side and NUMBER-only.
    assert dict(product.parameters)["Resistance"] == "1.2 kOhms"


def test_fetch_takes_the_mpn_before_the_digikey_id() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        seen["path"] = req.url.path
        return httpx.Response(200, json=_PRODUCT)

    DigiKeyProvider().fetch(
        "https://www.digikey.pl/pl/products/detail/walsin/MR04X1201FTL/13908146",
        transport=httpx.MockTransport(handler),
    )
    # The trailing all-digits segment is Digi-Key's own id, not the part number.
    assert "MR04X1201FTL/productdetails" in seen["path"]


def test_fetch_without_credentials_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "DIGIKEY_CLIENT_SECRET", "")
    with pytest.raises(ValidationError):
        DigiKeyProvider().fetch("https://www.digikey.com/x", transport=_transport())


def test_fetch_surfaces_a_token_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DigiKeyProvider().fetch(
            "https://www.digikey.com/x", transport=_transport(token_status=401)
        )
    assert "invalid_client" in str(excinfo.value)  # diagnosable, unlike a generic text


def test_fetch_redacts_credentials_from_an_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "DIGIKEY_CLIENT_SECRET", "super-secret")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad super-secret"})

    with pytest.raises(ValidationError) as excinfo:
        DigiKeyProvider().fetch(
            "https://www.digikey.com/x", transport=httpx.MockTransport(handler)
        )
    assert "super-secret" not in str(excinfo.value)


def test_fetch_when_no_product_in_the_response() -> None:
    with pytest.raises(ValidationError):
        DigiKeyProvider().fetch(
            "https://www.digikey.com/x", transport=_transport(product={})
        )


def test_fetch_rejects_a_non_dict_body() -> None:
    with pytest.raises(ValidationError):
        DigiKeyProvider().fetch(
            "https://www.digikey.com/x", transport=_transport(product=[1, 2])
        )


def test_token_is_cached_across_fetches() -> None:
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    url = "https://www.digikey.com/en/products/detail/w/MR04X1201FTL/13908146"
    DigiKeyProvider().fetch(url, transport=transport)
    DigiKeyProvider().fetch(url, transport=transport)
    assert calls["token"] == 1  # the short-lived token is reused, not re-requested

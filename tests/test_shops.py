"""Tests for shop-provider normalisation (create a component from a shop URL)."""

from __future__ import annotations

import json

import httpx
import pytest
from app import config
from app.services import shops
from app.services.errors import ValidationError
from app.services.shops.base import infer_category
from app.services.shops.mouser import MouserProvider

_MOUSER_OK = {
    "Errors": [],
    "SearchResults": {
        "NumberOfResult": 1,
        "Parts": [
            {
                "ManufacturerPartNumber": "CRCW040210K0FKED",
                "Manufacturer": "Vishay / Dale",
                "Description": "Thick Film Resistors - SMD 1/16watt 10Kohms 1%",
                "DataSheetUrl": "https://www.vishay.com/docs/20035/dcrcwe3.pdf",
                "Category": "Chip Resistor - Surface Mount",
                "ProductAttributes": [
                    {"AttributeName": "Resistance", "AttributeValue": "10 kOhms"},
                    {"AttributeName": "Tolerance", "AttributeValue": "±1%"},
                    {"AttributeName": "Power", "AttributeValue": "62.5 mW"},
                ],
            }
        ],
    },
}


def _transport(body: dict) -> httpx.MockTransport:  # type: ignore[type-arg]
    return httpx.MockTransport(lambda req: httpx.Response(200, json=body))


@pytest.mark.parametrize(
    "url",
    [
        "https://www.mouser.com/ProductDetail/x",
        "https://mouser.com/x",
        "https://www.mouser.pl/pl/ProductDetail/Walsin/MR04X1201FTL",  # country site
        "https://www.mouser.co.uk/x",  # multi-part TLD
        "https://eu.mouser.com/x",
    ],
)
def test_matches_mouser_hosts(url: str) -> None:
    assert MouserProvider().matches(url)


def test_does_not_match_another_shop() -> None:
    assert not MouserProvider().matches("https://www.digikey.com/x")


def test_fetch_uses_the_part_number_from_the_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_MOUSER_OK)

    MouserProvider().fetch(
        "https://www.mouser.pl/pl/ProductDetail/Walsin/MR04X1201FTL?qs=abc%3D%3D",
        transport=httpx.MockTransport(handler),
    )
    body = seen["body"]
    assert isinstance(body, dict)
    # The last path segment, with the query stripped.
    assert body["SearchByPartRequest"]["mouserPartNumber"] == "MR04X1201FTL"


def test_fetch_normalises_a_mouser_product(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    product = MouserProvider().fetch(
        "https://www.mouser.com/ProductDetail/Vishay/CRCW040210K0FKED",
        transport=_transport(_MOUSER_OK),
    )
    assert product.mpn == "CRCW040210K0FKED"
    assert product.manufacturer == "Vishay / Dale"
    assert product.datasheet_url and product.datasheet_url.endswith(".pdf")
    assert product.category == "resistor"  # inferred from "Chip Resistor…"
    # Values come through RAW; engineering cleaning is client-side and NUMBER-only.
    params = dict(product.parameters)
    assert params["Resistance"] == "10 kOhms"
    assert params["Tolerance"] == "±1%"
    assert params["Power"] == "62.5 mW"


def test_fetch_without_a_key_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "")
    with pytest.raises(ValidationError):
        MouserProvider().fetch(
            "https://www.mouser.com/x", transport=_transport(_MOUSER_OK)
        )


def test_fetch_surfaces_the_mouser_error_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    # A wrong/Order-API key gives exactly this; without the text it's undiagnosable.
    body = {
        "Errors": [{"Message": "Invalid unique identifier."}],
        "SearchResults": None,
    }
    with pytest.raises(ValidationError) as excinfo:
        MouserProvider().fetch("https://www.mouser.com/x", transport=_transport(body))
    assert "Invalid unique identifier" in str(excinfo.value)


def test_fetch_redacts_the_api_key_from_a_mouser_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "super-secret")
    body = {"Errors": [{"Message": "bad key super-secret"}], "SearchResults": None}
    with pytest.raises(ValidationError) as excinfo:
        MouserProvider().fetch("https://www.mouser.com/x", transport=_transport(body))
    assert "super-secret" not in str(excinfo.value)


def test_fetch_when_no_product_found(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    body = {"Errors": [], "SearchResults": {"NumberOfResult": 0, "Parts": []}}
    with pytest.raises(ValidationError):
        MouserProvider().fetch("https://www.mouser.com/x", transport=_transport(body))


def test_fetch_rejects_a_non_json_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>rate limited</html>")
    )
    with pytest.raises(ValidationError):  # JSONDecodeError → clean 422, not 500
        MouserProvider().fetch("https://www.mouser.com/x", transport=transport)


def test_fetch_rejects_a_non_dict_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(ValidationError):
        MouserProvider().fetch("https://www.mouser.com/x", transport=transport)


def test_matches_is_false_for_a_malformed_url() -> None:
    assert MouserProvider().matches("http://[::1/x") is False  # no crash


def test_lookup_rejects_an_unsupported_shop() -> None:
    with pytest.raises(ValidationError):
        shops.lookup("https://www.example.com/product/x")


def test_lookup_rejects_a_malformed_url() -> None:
    with pytest.raises(ValidationError):  # must be 422, not an unhandled 500
        shops.lookup("http://[::1/x")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Chip Resistor - Surface Mount", "resistor"),
        ("Multilayer Ceramic Capacitors MLCC", "capacitor"),
        ("Standard LEDs - SMD", "led"),
        ("Rectifier Diode", "diode"),
        ("MOSFET N-Channel", "mosfet"),
        ("Some Widget", None),
    ],
)
def test_infer_category(text: str, expected: str | None) -> None:
    assert infer_category(text) == expected

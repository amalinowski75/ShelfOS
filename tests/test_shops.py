"""Tests for shop-provider normalisation (create a component from a shop URL)."""

from __future__ import annotations

import httpx
import pytest
from app import config
from app.services import shops
from app.services.errors import ValidationError
from app.services.shops.base import clean_param_value, infer_category
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


def test_matches_mouser_host() -> None:
    provider = MouserProvider()
    assert provider.matches("https://www.mouser.com/ProductDetail/x")
    assert provider.matches("https://mouser.com/x")
    assert not provider.matches("https://www.digikey.com/x")


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
    params = dict(product.parameters)
    assert params["Resistance"] == "10k"
    assert params["Tolerance"] == "1"
    assert params["Power"] == "62.5m"


def test_fetch_without_a_key_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "")
    with pytest.raises(ValidationError):
        MouserProvider().fetch(
            "https://www.mouser.com/x", transport=_transport(_MOUSER_OK)
        )


def test_fetch_reports_a_mouser_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    body = {"Errors": [{"Message": "Invalid"}], "SearchResults": {"Parts": []}}
    with pytest.raises(ValidationError):
        MouserProvider().fetch("https://www.mouser.com/x", transport=_transport(body))


def test_fetch_when_no_product_found(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    body = {"Errors": [], "SearchResults": {"NumberOfResult": 0, "Parts": []}}
    with pytest.raises(ValidationError):
        MouserProvider().fetch("https://www.mouser.com/x", transport=_transport(body))


def test_lookup_rejects_an_unsupported_shop() -> None:
    with pytest.raises(ValidationError):
        shops.lookup("https://www.example.com/product/x")


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10 kOhms", "10k"),
        ("100 nF", "100n"),
        ("50 V", "50"),
        ("4.7 µF", "4.7u"),
        ("±5%", "5"),
        ("1 MHz", "1M"),
        ("SMD", "SMD"),  # no leading number → passthrough for the user to fix
    ],
)
def test_clean_param_value(raw: str, expected: str) -> None:
    assert clean_param_value(raw) == expected

"""Tests for the TME provider (create a component from a shop URL)."""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from app import config
from app.services.errors import ValidationError
from app.services.shops import tme
from app.services.shops.tme import TmeProvider

_URL = "https://www.tme.eu/pl/details/mr04x1201ftl/rezystory-smd-0402/walsin/"

_PRODUCT = {
    "status": "OK",
    "data": {
        "elements": [
            {
                "symbol": "MR04X1201FTL",
                "manufacturer_symbols": ["MR04X1201FTL"],
                "manufacturer": {"id": 1, "name": "Walsin Technology Corporation"},
                "description": "Resistor: thick film; SMD; 0402; 1.2kΩ; 63mW; ±1%",
                "category": {"id": 2, "name": "Resistors SMD 0402"},
            }
        ]
    },
}

_PARAMETERS = {
    "status": "OK",
    "data": {
        "elements": [
            {
                "symbol": "MR04X1201FTL",
                "parameters": {
                    "elements": [
                        # Duplicates a first-class field — must be dropped.
                        {"id": 2, "name": "Manufacturer", "values": [{"value": "Wal"}]},
                        {
                            "id": 10,
                            "name": "Resistance",
                            "values": [{"id": 1, "value": "1.2kΩ"}],
                        },
                        {
                            "id": 11,
                            "name": "Mounting",
                            "values": [{"value": "SMD"}, {"value": "THT"}],
                        },
                    ]
                },
            }
        ]
    },
}

_FILES = {
    "status": "OK",
    "data": {
        "elements": [
            {
                "symbol": "MR04X1201FTL",
                "documents": {
                    "elements": [
                        {"url": "//tme.eu/manual.pdf", "type": "INS"},
                        {"url": "//tme.eu/datasheet.pdf", "type": "DTE"},
                    ]
                },
            }
        ]
    },
}


@pytest.fixture(autouse=True)
def _creds_and_fresh_token(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "TME_TOKEN", "token")
    monkeypatch.setattr(config, "TME_SECRET", "secret")
    monkeypatch.setattr(tme, "_token_cache", None)  # never reuse across tests


def _transport(
    *,
    product: object = _PRODUCT,
    parameters: object = _PARAMETERS,
    files: object = _FILES,
    token_status: int = 200,
    parameters_status: int = 200,
    files_status: int = 200,
    seen: dict[str, httpx.Request] | None = None,
):
    """Routes the token POST and the three product GETs."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if seen is not None:
            seen[path] = req
        if path.endswith("/auth/token"):
            if token_status >= 400:
                return httpx.Response(token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 300}
            )
        if path.endswith("/products/parameters"):
            return httpx.Response(parameters_status, json=parameters)
        if path.endswith("/products/files"):
            return httpx.Response(files_status, json=files)
        return httpx.Response(200, json=product)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tme.eu/pl/details/1n4007-dio/x/y/",
        "https://tme.pl/pl/details/1n4007-dio/x/y/",
        "https://www.tme.pl/en/details/1n4007-dio/x/y/",
    ],
)
def test_matches_tme_hosts(url: str) -> None:
    assert TmeProvider().matches(url)


def test_does_not_match_another_shop() -> None:
    assert not TmeProvider().matches("https://www.mouser.com/x")


def test_matches_is_false_for_a_malformed_url() -> None:
    assert TmeProvider().matches("http://[::1/x") is False


def test_fetch_normalises_a_product() -> None:
    product = TmeProvider().fetch(_URL, transport=_transport())
    assert product.mpn == "MR04X1201FTL"
    assert product.manufacturer == "Walsin Technology Corporation"
    assert product.category == "resistor"  # inferred from "Resistors SMD 0402"
    # Values stay RAW; cleaning is client-side and NUMBER-only.
    assert dict(product.parameters)["Resistance"] == "1.2kΩ"
    # A multi-value parameter is joined rather than silently truncated.
    assert dict(product.parameters)["Mounting"] == "SMD, THT"
    # "Manufacturer" duplicates a first-class field and is not offered as a parameter.
    assert "Manufacturer" not in dict(product.parameters)


def test_fetch_prefers_the_documentation_file_and_makes_it_absolute() -> None:
    product = TmeProvider().fetch(_URL, transport=_transport())
    # DTE ("Documentation") wins over the INS manual listed before it, and the
    # protocol-relative URL is given a scheme so url_fetch can accept it.
    assert product.datasheet_url == "https://tme.eu/datasheet.pdf"


def test_fetch_falls_back_to_the_first_document_without_a_documentation_type() -> None:
    files = {
        "status": "OK",
        "data": {
            "elements": [
                {"documents": {"elements": [{"url": "//tme.eu/m.pdf", "type": "INS"}]}}
            ]
        },
    }
    product = TmeProvider().fetch(_URL, transport=_transport(files=files))
    assert product.datasheet_url == "https://tme.eu/m.pdf"


def test_fetch_uppercases_the_symbol_from_the_url() -> None:
    seen: dict[str, httpx.Request] = {}
    TmeProvider().fetch(
        "https://www.tme.eu/pl/details/1n4007-dio/diody/diotec/",
        transport=_transport(seen=seen),
    )
    # The URL carries the symbol lower-cased; the API expects it upper-cased.
    assert "symbols%5B%5D=1N4007-DIO" in str(seen["/products"].url)


def test_fetch_falls_back_to_the_tme_symbol_for_the_mpn() -> None:
    product = {
        "status": "OK",
        "data": {
            "elements": [
                {
                    "symbol": "AX-100",
                    "manufacturer_symbols": [],  # TME's own sample looks like this
                    "manufacturer": {"name": "AXIOMET"},
                    "description": "Digital multimeter",
                }
            ]
        },
    }
    imported = TmeProvider().fetch(_URL, transport=_transport(product=product))
    assert imported.mpn == "AX-100"


def test_fetch_survives_a_failing_parameters_call() -> None:
    product = TmeProvider().fetch(_URL, transport=_transport(parameters_status=500))
    # The import degrades rather than failing: core fields and the datasheet remain.
    assert product.mpn == "MR04X1201FTL"
    assert product.parameters == []
    assert product.datasheet_url == "https://tme.eu/datasheet.pdf"


def test_fetch_survives_a_failing_files_call() -> None:
    product = TmeProvider().fetch(_URL, transport=_transport(files_status=500))
    assert product.datasheet_url is None
    assert dict(product.parameters)["Resistance"] == "1.2kΩ"


def test_fetch_survives_an_empty_files_response() -> None:
    empty = {"status": "OK", "data": {"elements": []}}
    product = TmeProvider().fetch(_URL, transport=_transport(files=empty))
    assert product.datasheet_url is None


def test_fetch_keeps_an_already_absolute_datasheet_url() -> None:
    files = {
        "status": "OK",
        "data": {
            "elements": [
                {
                    "documents": {
                        "elements": [
                            {"url": "https://cdn.tme.eu/d.pdf", "type": "DTE"}
                        ]
                    }
                }
            ]
        },
    }
    product = TmeProvider().fetch(_URL, transport=_transport(files=files))
    assert product.datasheet_url == "https://cdn.tme.eu/d.pdf"  # not "https:https://"


def test_fetch_sends_the_configured_country() -> None:
    seen: dict[str, httpx.Request] = {}
    TmeProvider().fetch(_URL, transport=_transport(seen=seen))
    for path in ("/products", "/products/parameters", "/products/files"):
        assert f"country={config.TME_COUNTRY}" in str(seen[path].url)


@pytest.mark.parametrize("missing", ["TME_TOKEN", "TME_SECRET"])
def test_fetch_without_credentials_is_rejected(missing: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, missing, "")
    with pytest.raises(ValidationError):
        TmeProvider().fetch(_URL, transport=_transport())


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tme.eu/pl/",  # no "details" segment at all
        "https://www.tme.eu/pl/details/",  # "details" is the last segment
    ],
)
def test_fetch_rejects_a_url_without_a_symbol(url: str) -> None:
    with pytest.raises(ValidationError):
        TmeProvider().fetch(url, transport=_transport())


def test_fetch_surfaces_a_token_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TmeProvider().fetch(_URL, transport=_transport(token_status=401))
    assert "invalid_client" in str(excinfo.value)  # diagnosable, unlike a generic text


def test_fetch_redacts_the_secret_from_an_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "TME_SECRET", "super-secret")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad super-secret"})

    with pytest.raises(ValidationError) as excinfo:
        TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    assert "super-secret" not in str(excinfo.value)


def test_fetch_when_no_product_in_the_response() -> None:
    empty = {"status": "OK", "data": {"elements": []}}
    with pytest.raises(ValidationError):
        TmeProvider().fetch(_URL, transport=_transport(product=empty))


def test_fetch_rejects_a_non_dict_body() -> None:
    with pytest.raises(ValidationError):
        TmeProvider().fetch(_URL, transport=_transport(product=[1, 2]))


def test_token_is_cached_across_fetches() -> None:
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if path.endswith("/products/parameters"):
            return httpx.Response(200, json=_PARAMETERS)
        if path.endswith("/products/files"):
            return httpx.Response(200, json=_FILES)
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    TmeProvider().fetch(_URL, transport=transport)
    TmeProvider().fetch(_URL, transport=transport)
    assert calls["token"] == 1  # the short-lived token is reused, not re-requested


def test_token_is_refetched_once_it_expires() -> None:
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    TmeProvider().fetch(_URL, transport=transport)
    # Pretend the cached token is inside the 30s safety margin: it must not be reused.
    assert tme._token_cache is not None
    tme._token_cache = (tme._token_cache[0], time.monotonic() + 5)
    TmeProvider().fetch(_URL, transport=transport)
    assert calls["token"] == 2


def test_an_absurd_expiry_is_clamped() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 1e12})
        return httpx.Response(200, json=_PRODUCT)

    TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    assert tme._token_cache is not None
    # Otherwise a bogus expiry would pin a token TME has long since rotated.
    assert tme._token_cache[1] <= time.monotonic() + 3600


def test_a_null_expiry_still_yields_a_usable_token() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": None})
        return httpx.Response(200, json=_PRODUCT)

    # An explicit null must not throw a perfectly good token away.
    assert TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler)).mpn


def test_a_revoked_token_is_dropped_and_the_lookup_retried() -> None:
    calls = {"token": 0, "products": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(
                200, json={"access_token": f"tok{calls['token']}", "expires_in": 300}
            )
        if req.url.path == "/products":
            calls["products"] += 1
            if req.headers["Authorization"] == "Bearer tok1":
                return httpx.Response(401, json={"error": "token revoked"})
            return httpx.Response(200, json=_PRODUCT)
        return httpx.Response(200, json=_PARAMETERS)

    # Without eviction every import would fail until the cached expiry lapsed.
    product = TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    assert product.mpn == "MR04X1201FTL"
    assert (calls["token"], calls["products"]) == (2, 2)


def test_a_persistent_401_is_surfaced_not_retried_forever() -> None:
    calls = {"products": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        calls["products"] += 1
        return httpx.Response(401, json={"error": "nope"})

    with pytest.raises(ValidationError):
        TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    assert calls["products"] == 2  # one retry, then give up


def test_the_secret_is_redacted_from_logs(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "TME_SECRET", "super-secret")
    monkeypatch.setattr(config, "TME_TOKEN", "tok-en-50-chars")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "bad super-secret for tok-en-50-chars"}
        )

    with (
        caplog.at_level(logging.INFO, logger="shelfos"),
        pytest.raises(ValidationError),
    ):
        TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    # The log is the likelier leak channel, and BOTH halves are credentials.
    assert "super-secret" not in caplog.text
    assert "tok-en-50-chars" not in caplog.text


def test_token_is_sent_as_http_basic() -> None:
    seen: dict[str, httpx.Request] = {}
    TmeProvider().fetch(_URL, transport=_transport(seen=seen))
    # TME authenticates the token request with Basic, not with body credentials.
    assert seen["/auth/token"].headers["Authorization"].startswith("Basic ")
    assert seen["/products"].headers["Authorization"] == "Bearer tok"
    assert seen["/products"].headers["Accept-Language"] == config.TME_LANGUAGE

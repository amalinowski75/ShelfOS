"""Tests for the TME provider (create a component from a shop URL)."""

from __future__ import annotations

import logging
import math
import threading
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
    # The raw category is kept too: it often states the mounting where the
    # description doesn't (TME files 100nF parts under "MLCC SMD capacitors").
    assert product.shop_category == "Resistors SMD 0402"
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


def test_fetch_offers_every_url_segment_upper_cased_as_a_symbol() -> None:
    seen: dict[str, httpx.Request] = {}
    TmeProvider().fetch(_URL, transport=_transport(seen=seen))
    query = str(seen["/products"].url)
    # The URL carries the symbol lower-cased; the API expects it upper-cased. Its
    # POSITION is not fixed, so every plausible segment is offered at once and the
    # API decides which is real.
    assert "symbols%5B%5D=MR04X1201FTL" in query
    assert "symbols%5B%5D=WALSIN" in query
    # Over-long segments are dropped: TME fails the WHOLE request on one of them,
    # so "rezystory-smd-0402" (18) is fine but a longer slug would not be.
    assert "REZYSTORY-SMD-0402" in query
    # The enrichment calls use the RESOLVED symbol, never the candidate list.
    assert seen["/products/parameters"].url.params["symbols[]"] == "MR04X1201FTL"


def test_fetch_drops_segments_outside_tmes_symbol_length_limits() -> None:
    seen: dict[str, httpx.Request] = {}
    TmeProvider().fetch(
        "https://www.tme.eu/pl/details/mr04x1201ftl/kondensatory-mlcc-smd/x/",
        transport=_transport(seen=seen),
    )
    query = str(seen["/products"].url)
    # 21 characters: sending it would fail the whole lookup with a validation error
    # ("Product symbol should contain between 2 to 18 characters"), not just be
    # ignored — so the good symbol beside it would be lost too.
    assert "KONDENSATORY" not in query
    assert "symbols%5B%5D=X" not in query  # 1 character, below the minimum
    assert "symbols%5B%5D=MR04X1201FTL" in query


def test_fetch_finds_a_symbol_that_is_not_the_first_url_segment() -> None:
    # A real URL whose symbol sits SECOND; the first segment ("mpp2") is not a
    # symbol at all, and the last one is the manufacturer's part number.
    url = "https://www.tme.eu/pl/details/mpp2/681kd20jp10-yag/yageo/681kd20j-p10/"
    product = {
        "status": "OK",
        "data": {
            "elements": [
                {
                    "symbol": "681KD20JP10-YAG",
                    "manufacturer_symbols": ["681KD20J-P10"],
                    "manufacturer": {"name": "YAGEO"},
                    "description": "Diode: TVS",
                    "category": {"name": "TVS diodes"},
                }
            ]
        },
    }
    imported = TmeProvider().fetch(url, transport=_transport(product=product))
    assert imported.mpn == "681KD20J-P10"
    assert imported.category == "diode"


def test_an_ambiguous_url_prefers_the_product_whose_mpn_is_also_in_the_url() -> None:
    url = "https://www.tme.eu/pl/details/mpp2/681kd20jp10-yag/yageo/681kd20j-p10/"
    product = {
        "status": "OK",
        "data": {
            "elements": [
                # Both segments are live symbols. The tie-break is the MPN, which
                # the URL also carries — so the second element must win despite
                # coming later and its symbol appearing later in the URL.
                {"symbol": "MPP2", "manufacturer_symbols": ["SOMETHING-ELSE"]},
                {
                    "symbol": "681KD20JP10-YAG",
                    "manufacturer_symbols": ["681KD20J-P10"],
                    "description": "Diode: TVS",
                },
            ]
        },
    }
    imported = TmeProvider().fetch(url, transport=_transport(product=product))
    assert imported.mpn == "681KD20J-P10"


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
    imported = TmeProvider().fetch(
        "https://www.tme.eu/pl/details/ax-100/mierniki/axiomet/",
        transport=_transport(product=product),
    )
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


def test_fetch_resolves_a_host_relative_document_url() -> None:
    files = {
        "status": "OK",
        "data": {
            "elements": [
                {"documents": {"elements": [{"url": "/Document/x.pdf", "type": "DTE"}]}}
            ]
        },
    }
    product = TmeProvider().fetch(_URL, transport=_transport(files=files))
    # Left unnormalised it would fail the SSRF guard for an empty scheme, and the
    # user would be told the shop blocks downloads — which would be a lie.
    assert product.datasheet_url == "https://www.tme.eu/Document/x.pdf"


def test_the_manufacturer_parameter_is_filtered_whatever_type_its_id_has() -> None:
    parameters = {
        "status": "OK",
        "data": {
            "elements": [
                {
                    "parameters": {
                        "elements": [
                            # A JSON string id must filter the same as a number.
                            {
                                "id": "2",
                                "name": "Manufacturer",
                                "values": [{"value": "W"}],
                            },
                            {
                                "id": 10,
                                "name": "Resistance",
                                "values": [{"value": "1k"}],
                            },
                        ]
                    }
                }
            ]
        },
    }
    product = TmeProvider().fetch(_URL, transport=_transport(parameters=parameters))
    assert [n for n, _ in product.parameters] == ["Resistance"]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_an_enrichment_call_failing_never_fails_the_import(status: int) -> None:
    # Including 401/403: unlike the core lookup these deliberately do NOT retry with
    # a fresh token — they degrade, because the core call already succeeded.
    product = TmeProvider().fetch(
        _URL,
        transport=_transport(parameters_status=status, files_status=status),
    )
    assert product.mpn == "MR04X1201FTL"
    assert product.parameters == []
    assert product.datasheet_url is None


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("x"),
    ],
)
def test_a_network_failure_becomes_a_readable_error(exc: Exception) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(ValidationError) as excinfo:
        TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    # Never an unhandled 500, and never the exception text (it can embed the request).
    assert "could not reach TME" in str(excinfo.value)


def test_the_token_lock_is_not_held_across_the_network_call() -> None:
    """Asserts the property `_access_token`'s docstring claims.

    Holding the lock across the POST is not incorrect, just starving: every waiter
    serialises behind a full timeout when TME tarpits, stalling the shared sync
    worker pool. No behavioural test can see that, so check it directly.
    """
    held: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            acquired = tme._token_lock.acquire(blocking=False)
            held.append(not acquired)
            if acquired:
                tme._token_lock.release()
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        return httpx.Response(200, json=_PRODUCT)

    TmeProvider().fetch(_URL, transport=httpx.MockTransport(handler))
    assert held == [False]


def test_concurrent_fetches_leave_a_coherent_cache() -> None:
    issued: list[str] = []
    lock = threading.Lock()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            with lock:
                # A DISTINCT token per POST: with one shared value the test could
                # not tell a coherent cache from an incoherent one.
                token = f"tok{len(issued) + 1}"
                issued.append(token)
            time.sleep(0.01)  # widen the window for the accepted cold-start race
            return httpx.Response(200, json={"access_token": token, "expires_in": 300})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    results: list[str | None] = []

    def run() -> None:
        results.append(TmeProvider().fetch(_URL, transport=transport).mpn)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The cold-start race (several threads buying a token at once, a later write
    # possibly overwritten by an earlier one) is the accepted cost of releasing the
    # lock around the POST. It must stay benign: every caller succeeds and the cache
    # holds a token that was really issued, not a torn or stale value.
    assert results == ["MR04X1201FTL"] * 8
    assert issued, "no token was ever requested"
    assert tme._token_cache is not None
    assert tme._token_cache[0] in issued

    # And the cache is actually used: once warm, no further token is bought.
    before = len(issued)
    TmeProvider().fetch(_URL, transport=transport)
    assert len(issued) == before


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


def test_a_zero_expiry_is_not_treated_as_a_full_lifetime() -> None:
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 0})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    TmeProvider().fetch(_URL, transport=transport)
    TmeProvider().fetch(_URL, transport=transport)
    # 0 means "already expired" — a real OAuth answer, unlike NaN. Swallowing it
    # into a full lifetime would serve a dead token for the whole default TTL.
    assert calls["token"] == 2


def test_a_non_finite_expiry_does_not_disable_caching() -> None:
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(
                200, content=b'{"access_token": "t", "expires_in": NaN}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    TmeProvider().fetch(_URL, transport=transport)
    TmeProvider().fetch(_URL, transport=transport)
    # Python's json accepts the non-standard NaN literal, and NaN defeats min/max
    # (every comparison is False), so an unchecked value would make the cache look
    # permanently expired and silently disable caching for the process.
    assert calls["token"] == 1
    assert tme._token_cache is not None
    assert math.isfinite(tme._token_cache[1])


def test_eviction_does_not_discard_a_token_another_thread_just_cached() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "old", "expires_in": 300})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    TmeProvider().fetch(_URL, transport=transport)
    # Simulate a concurrent thread having refreshed the cache while ours was in
    # flight with the older token.
    tme._token_cache = ("newer", time.monotonic() + 600)
    tme._forget_token("old")
    # Compare-and-clear: the stale token is gone, but the newer one survives —
    # otherwise a key rotation amplifies into a burst of redundant token requests.
    assert tme._token_cache is not None
    assert tme._token_cache[0] == "newer"
    tme._forget_token("newer")
    assert tme._token_cache is None

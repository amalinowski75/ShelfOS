"""Tests for the Digi-Key provider (create a component from a shop URL)."""

from __future__ import annotations

import threading
import time

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


def test_token_is_refetched_once_it_expires() -> None:
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    url = "https://www.digikey.com/en/products/detail/w/MR04X1201FTL/13908146"
    DigiKeyProvider().fetch(url, transport=transport)
    # Pretend the cached token is inside the 30s safety margin: it must not be reused.
    assert digikey._token_cache is not None
    digikey._token_cache = (digikey._token_cache[0], time.monotonic() + 5)
    DigiKeyProvider().fetch(url, transport=transport)
    assert calls["token"] == 2


def test_an_absurd_expiry_is_clamped() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 1e12})
        return httpx.Response(200, json=_PRODUCT)

    DigiKeyProvider().fetch(
        "https://www.digikey.com/x", transport=httpx.MockTransport(handler)
    )
    assert digikey._token_cache is not None
    # Otherwise a bogus expiry would pin a token Digi-Key has long since rotated.
    assert digikey._token_cache[1] <= time.monotonic() + 3600


def test_a_null_expiry_still_yields_a_usable_token() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": None})
        return httpx.Response(200, json=_PRODUCT)

    # An explicit null must not throw a perfectly good token away.
    product = DigiKeyProvider().fetch(
        "https://www.digikey.com/x", transport=httpx.MockTransport(handler)
    )
    assert product.mpn == "MR04X1201FTL"


def test_a_revoked_token_is_dropped_and_the_lookup_retried() -> None:
    calls = {"token": 0, "products": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            calls["token"] += 1
            return httpx.Response(
                200, json={"access_token": f"tok{calls['token']}", "expires_in": 600}
            )
        calls["products"] += 1
        if req.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"detail": "token revoked"})
        return httpx.Response(200, json=_PRODUCT)

    # Without eviction every import would fail until the cached expiry lapsed —
    # up to ten minutes after a provider-side key rotation.
    product = DigiKeyProvider().fetch(
        "https://www.digikey.com/x", transport=httpx.MockTransport(handler)
    )
    assert product.mpn == "MR04X1201FTL"
    assert (calls["token"], calls["products"]) == (2, 2)


def test_a_persistent_401_is_surfaced_not_retried_forever() -> None:
    calls = {"products": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 600})
        calls["products"] += 1
        return httpx.Response(401, json={"detail": "nope"})

    with pytest.raises(ValidationError):
        DigiKeyProvider().fetch(
            "https://www.digikey.com/x", transport=httpx.MockTransport(handler)
        )
    assert calls["products"] == 2  # one retry, then give up


def test_the_token_lock_is_not_held_across_the_network_call() -> None:
    """The actual regression this backport is for.

    Holding the module lock across the token POST is not *incorrect* — it just
    serialises every waiter behind a full timeout when the shop tarpits, and since
    the lookup runs on the shared sync worker pool, that stalls unrelated endpoints.
    No behavioural test can see that, so assert the property directly: while the POST
    is in flight, the lock must be free.
    """
    held: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            acquired = digikey._token_lock.acquire(blocking=False)
            held.append(not acquired)
            if acquired:
                digikey._token_lock.release()
            return httpx.Response(200, json={"access_token": "t", "expires_in": 600})
        return httpx.Response(200, json=_PRODUCT)

    DigiKeyProvider().fetch(
        "https://www.digikey.com/x", transport=httpx.MockTransport(handler)
    )
    assert held == [False]


def test_concurrent_fetches_share_one_consistent_token() -> None:
    calls: list[int] = []
    lock = threading.Lock()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            with lock:
                calls.append(1)
            time.sleep(0.01)  # widen the window for the accepted cold-start race
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        return httpx.Response(200, json=_PRODUCT)

    transport = httpx.MockTransport(handler)
    results: list[str | None] = []

    def run() -> None:
        results.append(
            DigiKeyProvider().fetch(
                "https://www.digikey.com/x", transport=transport
            ).mpn
        )

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The token POST no longer happens under the lock, so a tarpitting Digi-Key
    # can't serialise every waiter. The resulting cold-start race must stay
    # harmless: every caller succeeds and the cache ends up coherent.
    assert results == ["MR04X1201FTL"] * 8
    assert len(calls) <= 8
    assert digikey._token_cache is not None and digikey._token_cache[0] == "tok"


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

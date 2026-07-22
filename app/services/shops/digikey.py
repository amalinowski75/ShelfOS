"""Digi-Key Product Information API provider (create a component from a shop URL).

Unlike Mouser's single api-key, Digi-Key uses OAuth2 client-credentials: the
ID/secret pair buys a short-lived access token, which we cache until it expires.
Both the token and product hosts are fixed constants, so there's no SSRF surface;
only the datasheet URL (arbitrary) is later fetched through the guarded url_fetch.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import ProductData, infer_category

_logger = logging.getLogger("shelfos")

_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None  # (access_token, expires_at monotonic)


def _redact(text: str) -> str:
    """Never let the client secret ride out in a message.

    Only the secret: the client id isn't sensitive (it goes out in a header), and
    redacting it would mangle unrelated text — a short id turns Digi-Key's own
    "invalid_client" into "inval***_client".
    """
    secret = config.DIGIKEY_CLIENT_SECRET
    return text.replace(secret, "***") if secret else text


def _error_text(payload: object) -> str:
    """Digi-Key's own error text — without it a bad credential is undiagnosable."""
    if isinstance(payload, dict):
        for key in ("detail", "title", "message", "error_description", "error"):
            value = payload.get(key)
            if value:
                return _redact(str(value))
    return "unknown error"


def _api_error(resp: httpx.Response, what: str) -> ValidationError:
    try:
        detail = _error_text(resp.json())
    except ValueError:
        detail = f"HTTP {resp.status_code}"
    _logger.warning("Digi-Key %s failed: %s", what, detail)
    return ValidationError(f"Digi-Key rejected the request: {detail}")


def _forget_token(stale: str) -> None:
    """Drop the cached token, but only if it is still the one that failed.

    Compare-and-clear, not an unconditional reset: a thread that gets a 401 for an
    old token must not discard a newer one another thread has meanwhile cached, or a
    key rotation would amplify into a burst of redundant token requests — the very
    situation this exists to smooth over.
    """
    global _token_cache
    with _token_lock:
        if _token_cache and _token_cache[0] == stale:
            _token_cache = None


def _access_token(client: httpx.Client) -> str:
    """A cached client-credentials token (Digi-Key's last ~10 minutes).

    The network call deliberately happens OUTSIDE the lock: holding it across a POST
    means that when Digi-Key is tarpitting, every waiting thread serialises behind a
    full-timeout request, and since the lookup runs on the shared sync worker pool
    that stalls unrelated endpoints too. The cost is that a cold start may buy two
    tokens concurrently, which is harmless.
    """
    global _token_cache
    with _token_lock:
        cached = _token_cache
    if cached and cached[1] > time.monotonic() + 30:  # small safety margin
        return cached[0]

    resp = client.post(
        f"{config.DIGIKEY_API_BASE}/v1/oauth2/token",
        data={
            "client_id": config.DIGIKEY_CLIENT_ID,
            "client_secret": config.DIGIKEY_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    if resp.status_code >= 400:
        raise _api_error(resp, "token request")
    try:
        payload = resp.json()
        token = str(payload["access_token"])
        raw = payload.get("expires_in")
        # An explicit null means 'unstated', so it takes the default — but a
        # literal 0 means 'already expired' and must survive as 0, which an
        # `or` would have swallowed.
        expires_in = float(600 if raw is None else raw)
    except (ValueError, KeyError, TypeError):
        raise ValidationError("could not read the Digi-Key token response") from None
    # Clamped: an absurd expiry would pin a token Digi-Key has long since
    # rotated. NaN is checked first because it defeats min/max entirely —
    # every comparison against it is False, so it would sail through and
    # make the cache permanently look expired, silently disabling caching.
    if not math.isfinite(expires_in):
        expires_in = float(600)
    expires_in = min(max(expires_in, 0.0), 3600.0)
    with _token_lock:
        _token_cache = (token, time.monotonic() + expires_in)
    return token


def _part_number(url: str) -> str:
    """The MPN from a Digi-Key product URL.

    They look like /en/products/detail/<manufacturer>/<MPN>/<digikey-id>, so the
    trailing all-digits segment is Digi-Key's own id and the MPN sits before it.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        raise ValidationError("malformed URL") from None
    segments = [s for s in path.split("/") if s]
    if not segments:
        raise ValidationError("could not read a part number from the URL")
    if len(segments) >= 2 and segments[-1].isdigit():
        return unquote(segments[-2])
    return unquote(segments[-1])


class DigiKeyProvider:
    name = "Digi-Key"

    def matches(self, url: str) -> bool:
        # Digi-Key runs many country sites (digikey.pl, digikey.de, digikey.co.uk…).
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        labels = host.split(".")
        return len(labels) > 1 and "digikey" in labels[:-1]

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        # The MPN is parsed from the URL; the API call is URL-independent, so a scan
        # reuses fetch_by_mpn.
        return self.fetch_by_mpn(_part_number(url), transport=transport)

    def fetch_by_mpn(
        self, mpn: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        """Look a part up by its manufacturer number directly (from a scan)."""
        if not (config.DIGIKEY_CLIENT_ID and config.DIGIKEY_CLIENT_SECRET):
            raise ValidationError("Digi-Key integration is not configured")
        # Escaped OUTSIDE the try below: a part number that can't be encoded (a lone
        # surrogate from a mangled scan) raises UnicodeEncodeError here, which that
        # try would either miss entirely — a 500 — or mislabel "could not reach".
        try:
            path_segment = quote(mpn, safe="", encoding="utf-8")
        except UnicodeEncodeError:
            raise ValidationError("could not read the part number") from None

        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:
                product_url = (
                    f"{config.DIGIKEY_API_BASE}/products/v4/search/"
                    f"{path_segment}/productdetails"
                )

                def _headers(token: str) -> dict[str, str]:
                    return {
                        "Authorization": f"Bearer {token}",
                        "X-DIGIKEY-Client-Id": config.DIGIKEY_CLIENT_ID,
                        "X-DIGIKEY-Locale-Site": config.DIGIKEY_LOCALE_SITE,
                        "X-DIGIKEY-Locale-Language": config.DIGIKEY_LOCALE_LANGUAGE,
                        "X-DIGIKEY-Locale-Currency": config.DIGIKEY_LOCALE_CURRENCY,
                    }

                token = _access_token(client)
                resp = client.get(product_url, headers=_headers(token))
                if resp.status_code in (401, 403):
                    # The cached token died early (rotated or revoked). Without this
                    # every import would fail until the cached expiry lapsed. The
                    # token is named so eviction can't discard a newer one.
                    _forget_token(token)
                    resp = client.get(
                        product_url, headers=_headers(_access_token(client))
                    )
                if resp.status_code >= 400:
                    raise _api_error(resp, "product lookup")
                payload = resp.json()
        except httpx.HTTPError:
            raise ValidationError("could not reach Digi-Key") from None

        if not isinstance(payload, dict):
            raise ValidationError("could not read the Digi-Key response")
        product = payload.get("Product")
        if not isinstance(product, dict):
            raise ValidationError("no product found")

        def _nested(key: str, field: str) -> str | None:
            value = product.get(key)
            return value.get(field) if isinstance(value, dict) else None

        parameters: list[tuple[str, str]] = []
        for param in product.get("Parameters") or []:
            if not isinstance(param, dict):
                continue
            name = (param.get("ParameterText") or param.get("Parameter") or "").strip()
            value = (param.get("ValueText") or param.get("Value") or "").strip()
            if name and value:
                parameters.append((name, value))  # raw; cleaned client-side per type

        category = _nested("Category", "Name")
        description = _nested("Description", "ProductDescription")
        return ProductData(
            # The product page from the response, not from the input: a scan looks a
            # part up by number and has no URL of its own, and this is what gets kept
            # as the component's shop link.
            source_url=product.get("ProductUrl") or None,
            mpn=product.get("ManufacturerProductNumber") or None,
            manufacturer=_nested("Manufacturer", "Name"),
            description=description,
            datasheet_url=product.get("DatasheetUrl") or None,
            category=infer_category(category, description),
            shop_category=category,
            parameters=parameters,
        )

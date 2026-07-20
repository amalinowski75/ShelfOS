"""TME API v2 provider (spec: create a component from a shop URL).

Like Digi-Key this is OAuth2 client-credentials, with two differences: the
credential pair travels as HTTP Basic rather than in the body, and the token lives
only 300 seconds — so the cache genuinely earns its keep. Product data comes from
three endpoints (core, parameters, files); only the first is required, so a shop-side
hiccup on the other two degrades the import instead of failing it.

Every host here is a fixed constant, so there's no SSRF surface; only the datasheet
URL (arbitrary) is later fetched through the guarded url_fetch.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import ProductData, infer_category

_logger = logging.getLogger("shelfos")

_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None  # (access_token, expires_at monotonic)

# TME's document types. DTE ("Documentation") is the closest thing they have to a
# datasheet; there is no dedicated datasheet type.
_DATASHEET_TYPE = "DTE"

# The "Manufacturer" parameter duplicates a first-class field, so it's dropped rather
# than offered as a component parameter.
_MANUFACTURER_PARAMETER_ID = 2


def _redact(text: str) -> str:
    """Never let either half of the credential pair ride out in a message.

    Unlike Digi-Key — where the client id is public and short enough that redacting
    it mangled unrelated text ("invalid_client" → "inval***_client") — TME's token is
    the username half of a Basic credential: 50 high-entropy characters that will
    never collide with prose. So both halves are scrubbed.
    """
    for secret in (config.TME_SECRET, config.TME_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def _error_text(payload: object) -> str:
    """TME's own error text — without it a bad credential is undiagnosable."""
    if isinstance(payload, dict):
        for key in ("error_description", "error", "message", "detail", "status"):
            value = payload.get(key)
            if value:
                return _redact(str(value))
    return "unknown error"


def _api_error(resp: httpx.Response, what: str) -> ValidationError:
    try:
        detail = _error_text(resp.json())
    except ValueError:
        detail = f"HTTP {resp.status_code}"
    _logger.warning("TME %s failed: %s", what, detail)
    return ValidationError(f"TME rejected the request: {detail}")


def _forget_token() -> None:
    """Drop the cached token so the next call buys a fresh one."""
    global _token_cache
    with _token_lock:
        _token_cache = None


def _access_token(client: httpx.Client) -> str:
    """A cached client-credentials token (TME's last ~300 seconds).

    The network call deliberately happens OUTSIDE the lock: holding it across a POST
    means that when TME is tarpitting, every waiting thread serialises behind a
    full-timeout request and the sync worker pool stalls for unrelated endpoints. The
    cost is that a cold start may buy two tokens concurrently, which is harmless.
    """
    global _token_cache
    with _token_lock:
        cached = _token_cache
    if cached and cached[1] > time.monotonic() + 30:  # small safety margin
        return cached[0]

    resp = client.post(
        f"{config.TME_API_BASE}/auth/token",
        data={"grant_type": "client_credentials"},
        # The pair is HTTP Basic here, not body fields as with Digi-Key.
        auth=(config.TME_TOKEN, config.TME_SECRET),
    )
    if resp.status_code >= 400:
        raise _api_error(resp, "token request")
    try:
        payload = resp.json()
        token = str(payload["access_token"])
        # `or` not a get() default: an explicit "expires_in": null would otherwise
        # reach float() and throw away a perfectly good token.
        expires_in = float(payload.get("expires_in") or 300)
    except (ValueError, KeyError, TypeError):
        raise ValidationError("could not read the TME token response") from None
    # Clamped: an absurd expiry would pin a token TME has long since rotated.
    expires_in = min(max(expires_in, 0.0), 3600.0)
    with _token_lock:
        _token_cache = (token, time.monotonic() + expires_in)
    return token


def _symbol(url: str) -> str:
    """The TME symbol from a product URL.

    They look like /pl/details/<symbol>/<category-slug>/<producer-slug>/, and the URL
    carries the symbol lower-cased while the API expects it upper-cased
    ("…/details/1n4007-dio/" → "1N4007-DIO"). Note this is TME's own symbol, not the
    manufacturer's part number — that comes back from the API.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        raise ValidationError("malformed URL") from None
    segments = [s for s in path.split("/") if s]
    try:
        index = segments.index("details")
    except ValueError:
        raise ValidationError(
            "could not read a product symbol from the URL"
        ) from None
    if index + 1 >= len(segments):
        raise ValidationError("could not read a product symbol from the URL")
    symbol = unquote(segments[index + 1]).upper()
    if not symbol:
        raise ValidationError("could not read a product symbol from the URL")
    return symbol


def _first_element(payload: object) -> dict[str, Any]:
    """The single product out of TME's {"status", "data": {"elements": [...]}}."""
    if not isinstance(payload, dict):
        raise ValidationError("could not read the TME response")
    data = payload.get("data")
    elements = data.get("elements") if isinstance(data, dict) else None
    if not isinstance(elements, list) or not elements:
        raise ValidationError("no product found for this URL")
    element = elements[0]
    if not isinstance(element, dict):
        raise ValidationError("could not read the TME response")
    return element


def _text(value: object) -> str | None:
    """A shop-supplied scalar as a string, or None — never another type."""
    return value if isinstance(value, str) and value else None


def _nested_name(container: object) -> str | None:
    """``{"id": .., "name": ..}`` → the name."""
    return _text(container.get("name")) if isinstance(container, dict) else None


def _absolute(url: str) -> str:
    """TME returns protocol-relative asset URLs; url_fetch needs a real scheme.

    This does NOT sanitise the URL, and must not be relied on to: the value is
    shop-controlled and is only ever handed to ``POST /api/attachments/from-url``,
    which validates the scheme and guards against SSRF (``app/services/url_fetch``).
    If this field ever becomes something rendered as an href, it needs escaping there.
    """
    return f"https:{url}" if url.startswith("//") else url


def _parameters(payload: object) -> list[tuple[str, str]]:
    """``data.elements[0].parameters.elements[]`` → (name, value) pairs, raw."""
    element = _first_element(payload)
    container = element.get("parameters")
    entries = container.get("elements") if isinstance(container, dict) else None
    parameters: list[tuple[str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("id") == _MANUFACTURER_PARAMETER_ID:
            continue
        name = str(entry.get("name") or "").strip()
        values = [
            str(v.get("value") or "").strip()
            for v in entry.get("values") or []
            if isinstance(v, dict) and v.get("value")
        ]
        if name and values:
            # Values stay raw: engineering cleaning is client-side and NUMBER-only.
            parameters.append((name, ", ".join(values)))
    return parameters


def _datasheet_url(payload: object) -> str | None:
    """The best datasheet candidate out of ``documents.elements[]``."""
    element = _first_element(payload)
    container = element.get("documents")
    entries = container.get("elements") if isinstance(container, dict) else None
    documents = [
        d for d in entries or [] if isinstance(d, dict) and str(d.get("url") or "")
    ]
    if not documents:
        return None
    for document in documents:
        if document.get("type") == _DATASHEET_TYPE:
            return _absolute(str(document["url"]))
    return _absolute(str(documents[0]["url"]))  # no Documentation — take what we have


class TmeProvider:
    name = "TME"

    def matches(self, url: str) -> bool:
        # TME runs tme.eu and tme.pl (and www. in front of either), so match "tme" as
        # a domain label rather than one fixed host. A false match (tme.example.com)
        # is harmless: fetch() discards the host entirely and only ever calls the
        # fixed TME_API_BASE, so the worst case is "no product found for this URL".
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:  # e.g. an unbalanced-bracket IPv6 literal
            return False
        labels = host.split(".")
        return len(labels) > 1 and "tme" in labels[:-1]

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        if not (config.TME_TOKEN and config.TME_SECRET):
            raise ValidationError("TME integration is not configured")
        symbol = _symbol(url)
        params = {"symbols[]": symbol, "country": config.TME_COUNTRY}

        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:
                def _headers() -> dict[str, str]:
                    return {
                        "Authorization": f"Bearer {_access_token(client)}",
                        "Accept-Language": config.TME_LANGUAGE,
                    }

                headers = _headers()
                resp = client.get(
                    f"{config.TME_API_BASE}/products", params=params, headers=headers
                )
                if resp.status_code in (401, 403):
                    # The cached token died early (rotated or revoked). Without this
                    # every import would fail until the cached expiry lapsed.
                    _forget_token()
                    headers = _headers()
                    resp = client.get(
                        f"{config.TME_API_BASE}/products",
                        params=params,
                        headers=headers,
                    )
                if resp.status_code >= 400:
                    raise _api_error(resp, "product lookup")
                product = _first_element(resp.json())

                def _extra(path: str) -> Any:
                    resp = client.get(
                        f"{config.TME_API_BASE}{path}", params=params, headers=headers
                    )
                    resp.raise_for_status()
                    return resp.json()

                # Parameters and documents only enrich the import: if either call
                # fails the user still gets a pre-filled dialog, just a thinner one.
                parameters: list[tuple[str, str]] = []
                datasheet_url: str | None = None
                try:
                    parameters = _parameters(_extra("/products/parameters"))
                except (httpx.HTTPError, ValueError, ValidationError) as exc:
                    _logger.info("TME parameters skipped for %s: %s", symbol, exc)
                try:
                    datasheet_url = _datasheet_url(_extra("/products/files"))
                except (httpx.HTTPError, ValueError, ValidationError) as exc:
                    _logger.info("TME documents skipped for %s: %s", symbol, exc)
        except httpx.HTTPError:
            # Never surface the exception itself — it can embed request details.
            raise ValidationError("could not reach TME") from None
        except ValueError:  # a non-JSON body from the core call
            raise ValidationError("could not read the TME response") from None

        # Everything below is shop-controlled JSON, so no field's type is assumed.
        # A bare string where a list belongs would otherwise be iterated character
        # by character and yield an mpn of "M".
        symbols = product.get("manufacturer_symbols")
        # TME's own sample product has an empty list, so the fallback matters.
        mpn = _text(product.get("symbol"))
        if isinstance(symbols, list):
            mpn = next((s for s in symbols if isinstance(s, str) and s), mpn)
        description = _text(product.get("description"))
        category = _nested_name(product.get("category"))
        return ProductData(
            mpn=mpn,
            manufacturer=_nested_name(product.get("manufacturer")),
            description=description,
            datasheet_url=datasheet_url,
            category=infer_category(category, description),
            parameters=parameters,
        )

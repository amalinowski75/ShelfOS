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
import math
import re
import threading
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import ProductData, ShopLookupMiss, infer_category

_logger = logging.getLogger("shelfos")

_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None  # (access_token, expires_at monotonic)

# TME's document types. DTE ("Documentation") is the closest thing they have to a
# datasheet; there is no dedicated datasheet type.
_DATASHEET_TYPE = "DTE"

# Documents live on the storefront host, not the API host, so a host-relative
# document path resolves against this rather than TME_API_BASE.
_DOCUMENT_BASE = "https://www.tme.eu/"

# The "Manufacturer" parameter duplicates a first-class field, so it's dropped rather
# than offered as a component parameter.
_MANUFACTURER_PARAMETER_ID = "2"

# TME rejects the whole request — not just the offending entry — when any symbol
# falls outside this range ("Product symbol should contain between 2 to 18
# characters"), so candidates are filtered before they are sent.
_MIN_SYMBOL = 2
_MAX_SYMBOL = 18


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
        raw = payload.get("expires_in")
        # An explicit null means 'unstated', so it takes the default — but a
        # literal 0 means 'already expired' and must survive as 0, which an
        # `or` would have swallowed.
        expires_in = float(300 if raw is None else raw)
    except (ValueError, KeyError, TypeError):
        raise ValidationError("could not read the TME token response") from None
    # Clamped: an absurd expiry would pin a token TME has long since
    # rotated. NaN is checked first because it defeats min/max entirely —
    # every comparison against it is False, so it would sail through and
    # make the cache permanently look expired, silently disabling caching.
    if not math.isfinite(expires_in):
        expires_in = float(300)
    expires_in = min(max(expires_in, 0.0), 3600.0)
    with _token_lock:
        _token_cache = (token, time.monotonic() + expires_in)
    return token


def _symbol_candidates(url: str) -> list[str]:
    """Every path segment that could be the TME symbol, in URL order.

    The symbol's POSITION is not fixed. Both of these are real product URLs, with
    the symbol first in one and second in the other:

        /pl/details/0603b104k500ct/kondensatory-mlcc-smd/walsin/
        /pl/details/mpp2/681kd20jp10-yag/yageo/681kd20j-p10/

    So rather than guess an index, every segment is offered to the API at once — it
    accepts up to 50 symbols and silently omits the ones that don't exist, which
    makes it a far better oracle than any parsing rule. Segments are upper-cased
    (URLs carry the symbol lower-cased) and filtered to TME's documented 2..18
    characters: an over-long segment such as "kondensatory-mlcc-smd" is not merely
    ignored by the API, it fails the WHOLE request with a validation error.

    Note the symbol is TME's own, not the manufacturer's part number — the URL's
    last segment is often the MPN and is deliberately not treated as a symbol.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        raise ValidationError("malformed URL") from None
    segments = [s for s in path.split("/") if s]
    try:
        # Case-insensitively: an all-upper-case URL (QR alphanumeric mode can encode
        # nothing else) is the same product page, and the host match is already
        # case-insensitive, so the path comparison must not be the odd one out.
        index = [s.lower() for s in segments].index("details")
    except ValueError:
        raise ValidationError("could not read a product symbol from the URL") from None

    candidates = _usable_symbols(unquote(s) for s in segments[index + 1 :])
    if not candidates:
        raise ValidationError("could not read a product symbol from the URL")
    return candidates


# A conservative sketch of what TME accepts in a symbol: letters, digits and the
# separators seen in real symbols. One rejected symbol fails the WHOLE batched
# request — losing the valid candidates in it — so anything outside this set (a
# comma-suffixed MPN like "PESD5V0S1BA,115", prose, whitespace) is dropped up front.
_SYMBOL_CHARS = re.compile(r"[A-Z0-9._/+-]+")


def _usable_symbols(raw: Iterable[str]) -> list[str]:
    """Normalise candidate symbols: strip/upper-case, filter, dedupe (order kept).

    The ONE pipeline for both candidate sources (URL segments and invoice numbers),
    so the URL and invoice paths can't drift apart on what counts as a symbol.
    Filtered to TME's documented 2..18 characters and the accepted charset.
    """
    candidates: list[str] = []
    for entry in raw:
        symbol = entry.strip().upper()
        usable = (
            _MIN_SYMBOL <= len(symbol) <= _MAX_SYMBOL
            and _SYMBOL_CHARS.fullmatch(symbol) is not None
        )
        if usable and symbol not in candidates:
            candidates.append(symbol)
    return candidates


def _pick_product(payload: object, candidates: list[str]) -> dict[str, Any]:
    """The one real product among the candidates we offered.

    Ambiguity is possible in principle (two segments both being live symbols), so
    the tie-break is deterministic: prefer a product whose manufacturer part number
    also appears in the URL — a strong signal, since these URLs carry both — and
    otherwise take the earliest candidate in URL order.
    """
    if not isinstance(payload, dict):
        raise ValidationError("could not read the TME response")
    data = payload.get("data")
    elements = data.get("elements") if isinstance(data, dict) else None
    found = {
        str(e["symbol"]).upper(): e
        for e in elements or []
        if isinstance(e, dict) and e.get("symbol")
    }
    matched = [c for c in candidates if c in found]
    if not matched:
        # Candidate-neutral: this raise serves both the URL path and the invoice
        # path (which has no URL at all).
        raise ShopLookupMiss("no product found for the offered symbols")
    for candidate in matched:
        mpns = found[candidate].get("manufacturer_symbols")
        if isinstance(mpns, list) and any(
            isinstance(m, str) and m.upper() in candidates for m in mpns
        ):
            return found[candidate]
    return found[matched[0]]


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
    """Give a document URL a scheme and host; url_fetch rejects anything without.

    Observed values are protocol-relative ("//www.tme.eu/Document/…"), but urljoin
    also covers a host-relative "/Document/…" — which would otherwise reach the user
    as the misleading "the shop blocks automated downloads" notice when the real
    cause is a URL we failed to normalise. An already-absolute URL is left alone.

    This does NOT sanitise the URL, and must not be relied on to: the value is
    shop-controlled and is only ever handed to ``POST /api/attachments/from-url``,
    which validates the scheme and guards against SSRF (``app/services/url_fetch``).
    If this field ever becomes something rendered as an href, it needs escaping there.
    """
    return urljoin(_DOCUMENT_BASE, url)


def _parameters(payload: object) -> list[tuple[str, str]]:
    """``data.elements[0].parameters.elements[]`` → (name, value) pairs, raw."""
    element = _first_element(payload)
    container = element.get("parameters")
    entries = container.get("elements") if isinstance(container, dict) else None
    parameters: list[tuple[str, str]] = []
    for entry in entries or []:
        # Compared as text: TME returns the id as a number today, but a JSON
        # "2" must filter the same or the row leaks through as a duplicate.
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id")) == _MANUFACTURER_PARAMETER_ID:
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


def url_symbols(url: str) -> list[str]:
    """Symbol candidates in a product URL, or ``[]`` if it carries none.

    The forgiving face of :func:`_symbol_candidates`, for callers that merely
    want identifiers to match against data they already hold (a scanned QR
    against a draft invoice's lines) rather than a product to look up — a URL
    with no readable symbol is an empty answer there, not an error.
    """
    try:
        return _symbol_candidates(url)
    except ValidationError:
        return []


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

    def product_url(self, part_number: str) -> str:
        # The invoice's supplier-part-number for TME IS the TME symbol, so it maps
        # straight to the storefront's product page (case-insensitive there).
        return f"https://www.tme.eu/en/details/{quote(part_number, safe='')}/"

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        # The URL is only a carrier for symbol candidates; the API call itself is
        # URL-independent, so an invoice import reuses fetch_by_symbols directly.
        if not (config.TME_TOKEN and config.TME_SECRET):
            raise ValidationError("TME integration is not configured")
        return self.fetch_by_symbols(_symbol_candidates(url), transport=transport)

    def fetch_by_index(
        self,
        candidates: list[str],
        *,
        manufacturer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ProductData:
        """Uniform invoice-import entry: TME's index IS its symbol (one API call).

        ``manufacturer`` is accepted for the uniform provider interface; TME resolves
        one product per symbol, so there is nothing to disambiguate.

        ``mpn_from_symbol=False``: on this path a parsed MPN from the invoice exists
        and is accurate ("Symbol producenta:"), so when the API's answer carries no
        manufacturer symbol the ProductData must NOT substitute TME's own catalogue
        symbol for it — the orchestrator's ``product.mpn or line.mpn`` would then
        overwrite the correct parsed MPN with a shop-internal number.
        """
        return self.fetch_by_symbols(
            candidates, transport=transport, mpn_from_symbol=False
        )

    def fetch_by_symbols(
        self,
        symbols: list[str],
        *,
        transport: httpx.BaseTransport | None = None,
        mpn_from_symbol: bool = True,
    ) -> ProductData:
        """Look a product up by candidate TME symbols directly (invoice import).

        Candidates are normalised by ``_usable_symbols`` (the same pipeline as the
        URL path) — one bad symbol fails the WHOLE request, not just its own entry.
        The API silently omits candidates that don't exist, so offering several (a
        possibly-truncated invoice article plus the MPN) and letting it pick is the
        same oracle trick the URL path uses. ``mpn_from_symbol`` keeps the URL/scan
        behaviour of falling back to TME's own symbol when the product carries no
        manufacturer symbol; the invoice path disables it (see fetch_by_index).
        """
        if not (config.TME_TOKEN and config.TME_SECRET):
            raise ValidationError("TME integration is not configured")
        candidates = _usable_symbols(symbols)
        if not candidates:
            raise ValidationError("no usable TME symbol to look up")
        # A list of pairs, not a dict: the same key repeats once per candidate.
        lookup_params: list[tuple[str, str | int | float | bool | None]] = [
            ("symbols[]", c) for c in candidates
        ]
        lookup_params.append(("country", config.TME_COUNTRY))

        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:

                def _headers(token: str) -> dict[str, str]:
                    return {
                        "Authorization": f"Bearer {token}",
                        "Accept-Language": config.TME_LANGUAGE,
                    }

                token = _access_token(client)
                headers = _headers(token)
                resp = client.get(
                    f"{config.TME_API_BASE}/products",
                    params=lookup_params,
                    headers=headers,
                )
                if resp.status_code in (401, 403):
                    # The cached token died early (rotated or revoked). Without this
                    # every import would fail until the cached expiry lapsed. The
                    # token is named so eviction can't discard a newer one.
                    _forget_token(token)
                    headers = _headers(_access_token(client))
                    resp = client.get(
                        f"{config.TME_API_BASE}/products",
                        params=lookup_params,
                        headers=headers,
                    )
                if resp.status_code >= 400:
                    raise _api_error(resp, "product lookup")
                product = _pick_product(resp.json(), candidates)
                # Which candidate was real is only known now; the enrichment calls
                # must use that resolved symbol, not the ones we guessed.
                symbol = str(product.get("symbol") or "")
                params = {"symbols[]": symbol, "country": config.TME_COUNTRY}

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
                    _logger.warning("TME parameters skipped for %s: %s", symbol, exc)
                try:
                    datasheet_url = _datasheet_url(_extra("/products/files"))
                except (httpx.HTTPError, ValueError, ValidationError) as exc:
                    _logger.warning("TME documents skipped for %s: %s", symbol, exc)
        except httpx.HTTPError:
            # Never surface the exception itself — it can embed request details.
            raise ValidationError("could not reach TME") from None
        except ValueError:  # a non-JSON body from the core call
            raise ValidationError("could not read the TME response") from None

        # Everything below is shop-controlled JSON, so no field's type is assumed.
        # A bare string where a list belongs would otherwise be iterated character
        # by character and yield an mpn of "M".
        mfr_symbols = product.get("manufacturer_symbols")
        # TME's own sample product has an empty list, so the URL/scan path falls back
        # to TME's own symbol (better than nothing there); the invoice path must not
        # (mpn_from_symbol=False) — its parsed MPN is accurate and must win instead.
        mpn = _text(product.get("symbol")) if mpn_from_symbol else None
        if isinstance(mfr_symbols, list):
            mpn = next((s for s in mfr_symbols if isinstance(s, str) and s), mpn)
        description = _text(product.get("description"))
        category = _nested_name(product.get("category"))
        return ProductData(
            mpn=mpn,
            manufacturer=_nested_name(product.get("manufacturer")),
            description=description,
            datasheet_url=datasheet_url,
            category=infer_category(category, description),
            shop_category=category,
            parameters=parameters,
        )

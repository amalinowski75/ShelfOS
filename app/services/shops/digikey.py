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
from app.services.shops.base import (
    ProductData,
    ShopLookupMiss,
    fetch_first_match,
    infer_category,
    manufacturer_matches,
)

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


def _product_data(product: dict[str, object]) -> ProductData:
    """Normalise a Digi-Key v4 ``Product`` object into :class:`ProductData`.

    Shared by the two endpoints that return one: ``productdetails`` (single result)
    and ``keyword`` (a list we've already picked from) use the same product schema,
    so both parse here. Missing optional sections (Parameters, Category) degrade to
    empty/None rather than failing.
    """

    def _nested(key: str, field: str) -> str | None:
        value = product.get(key)
        return value.get(field) if isinstance(value, dict) else None

    parameters: list[tuple[str, str]] = []
    raw_params = product.get("Parameters")
    for param in raw_params if isinstance(raw_params, list) else []:
        if not isinstance(param, dict):
            continue
        name = (param.get("ParameterText") or param.get("Parameter") or "").strip()
        value = (param.get("ValueText") or param.get("Value") or "").strip()
        if name and value:
            parameters.append((name, value))  # raw; cleaned client-side per type

    category = _nested("Category", "Name")
    description = _nested("Description", "ProductDescription")
    source_url = product.get("ProductUrl")
    mpn = product.get("ManufacturerProductNumber")
    datasheet_url = product.get("DatasheetUrl")
    return ProductData(
        # The product page from the response, not from the input: a scan looks a
        # part up by number and has no URL of its own, and this is what gets kept
        # as the component's shop link.
        source_url=source_url if isinstance(source_url, str) and source_url else None,
        mpn=mpn if isinstance(mpn, str) and mpn else None,
        manufacturer=_nested("Manufacturer", "Name"),
        description=description,
        datasheet_url=(
            datasheet_url if isinstance(datasheet_url, str) and datasheet_url else None
        ),
        category=infer_category(category, description),
        shop_category=category,
        parameters=parameters,
    )


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

    def product_url(self, part_number: str) -> str:
        # The direct product path needs Digi-Key's internal id; the keyword search
        # takes the part number alone and lands an exact one on its product page.
        return f"https://www.digikey.com/en/products/result?keywords={quote(part_number)}"

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        # The MPN is parsed from the URL; the API call is URL-independent, so a scan
        # reuses fetch_by_mpn.
        return self.fetch_by_mpn(_part_number(url), transport=transport)

    def fetch_by_index(
        self,
        candidates: list[str],
        *,
        manufacturer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ProductData:
        """Try each candidate number in order; first hit wins (invoice import).

        The invoice's Digi-Key number ("…-ND") comes first — the product-details
        endpoint's path parameter is Digi-Key's own part number, with MPN accepted as
        a best-match fallback — then the parsed MPN (see ``fetch_first_match`` for
        the miss-only fallthrough and error aggregation). ``manufacturer`` (the
        invoice line's) breaks ties when a bare MPN is sold under several makers.
        """
        return fetch_first_match(
            lambda number: self.fetch_by_mpn(
                number, manufacturer=manufacturer, transport=transport
            ),
            candidates,
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": config.DIGIKEY_CLIENT_ID,
            "X-DIGIKEY-Locale-Site": config.DIGIKEY_LOCALE_SITE,
            "X-DIGIKEY-Locale-Language": config.DIGIKEY_LOCALE_LANGUAGE,
            "X-DIGIKEY-Locale-Currency": config.DIGIKEY_LOCALE_CURRENCY,
        }

    def _authed_json(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        json: object = None,
    ) -> dict[str, object]:
        """One authenticated request, retrying once on an early-expired token.

        Shared by the productdetails GET and the keyword POST so the token dance
        (acquire, retry on 401/403 with a named token so eviction can't discard a
        newer one) lives in one place. Non-JSON or a non-object body is a clean
        ValidationError, like Mouser's and TME's — never a 500.
        """
        token = _access_token(client)
        resp = client.request(method, url, headers=self._headers(token), json=json)
        if resp.status_code in (401, 403):
            _forget_token(token)
            resp = client.request(
                method, url, headers=self._headers(_access_token(client)), json=json
            )
        if resp.status_code >= 400:
            raise _api_error(resp, "product lookup")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValidationError("could not read the Digi-Key response")
        return payload

    def _search_for_maker(
        self, client: httpx.Client, mpn: str, manufacturer: str
    ) -> dict[str, object] | None:
        """The exact-MPN KeywordSearch hit whose maker matches, or None.

        ``productdetails`` resolves a SINGLE product for a number, so for a bare MPN
        that several makers share ("5120" is Keystone's AND ABB's) it can only return
        Digi-Key's own pick — the wrong company, silently. KeywordSearch returns the
        whole candidate list, so this is where the scanned/invoiced manufacturer
        chooses. Returns None (fall back to productdetails) when nothing matches.
        """
        payload = self._authed_json(
            client,
            "POST",
            f"{config.DIGIKEY_API_BASE}/products/v4/search/keyword",
            json={"Keywords": mpn},
        )
        queried = mpn.strip().casefold()
        # ExactMatches is Digi-Key's own exact-number bucket; the general Products
        # list is the fallback. Within each, require the manufacturer number to equal
        # what we asked (KeywordSearch also returns near matches) AND the maker to
        # match the scan.
        for bucket in ("ExactMatches", "Products"):
            entries = payload.get(bucket)
            for product in entries if isinstance(entries, list) else []:
                if not isinstance(product, dict):
                    continue
                number = str(product.get("ManufacturerProductNumber") or "")
                if number.strip().casefold() != queried:
                    continue
                maker = product.get("Manufacturer")
                name = maker.get("Name") if isinstance(maker, dict) else None
                if manufacturer_matches(manufacturer, name):
                    return product
        return None

    def fetch_by_mpn(
        self,
        mpn: str,
        *,
        manufacturer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ProductData:
        """Look a part up by its manufacturer number directly (from a scan).

        ``productdetails`` is the authoritative lookup and runs first, as before — so
        an unambiguous number (and the invoice's Digi-Key SKU, which productdetails
        resolves directly) stays a single call. Only when a manufacturer was given AND
        the answer comes back bearing a DIFFERENT one is the number ambiguous: Digi-Key
        picked for us, so KeywordSearch (which returns the whole candidate list) is
        consulted to correct it. That correction is best-effort — its own failure keeps
        the productdetails answer rather than sinking the lookup.
        """
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
                payload = self._authed_json(
                    client,
                    "GET",
                    f"{config.DIGIKEY_API_BASE}/products/v4/search/"
                    f"{path_segment}/productdetails",
                )
                detail = payload.get("Product")
                if not isinstance(detail, dict):
                    raise ShopLookupMiss("no product found")
                product = _product_data(detail)
                if manufacturer and not manufacturer_matches(
                    manufacturer, product.manufacturer
                ):
                    # Ambiguous: productdetails returned a maker we didn't ask for.
                    # Correct via KeywordSearch — best-effort, so ANY failure of this
                    # optional step (an outage, a non-JSON body) keeps the answer in
                    # hand rather than disabling enrichment for the whole invoice.
                    try:
                        match = self._search_for_maker(client, mpn, manufacturer)
                    except (ValidationError, httpx.HTTPError, ValueError) as exc:
                        _logger.info(
                            "Digi-Key KeywordSearch unavailable, keeping the "
                            "productdetails answer: %s",
                            exc,
                        )
                        match = None
                    if match is not None:
                        return _product_data(match)
                return product
        except httpx.HTTPError:
            raise ValidationError("could not reach Digi-Key") from None
        except ValueError:
            # A 200 carrying a non-JSON body (a proxy/WAF page) → a clean
            # ValidationError like Mouser's and TME's, not a sunk import.
            raise ValidationError("could not read the Digi-Key response") from None

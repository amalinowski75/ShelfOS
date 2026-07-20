"""Digi-Key Product Information API provider (create a component from a shop URL).

Unlike Mouser's single api-key, Digi-Key uses OAuth2 client-credentials: the
ID/secret pair buys a short-lived access token, which we cache until it expires.
Both the token and product hosts are fixed constants, so there's no SSRF surface;
only the datasheet URL (arbitrary) is later fetched through the guarded url_fetch.
"""

from __future__ import annotations

import logging
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


def _access_token(client: httpx.Client) -> str:
    """A cached client-credentials token (Digi-Key's last ~10 minutes)."""
    global _token_cache
    with _token_lock:
        now = time.monotonic()
        if _token_cache and _token_cache[1] > now + 30:  # small safety margin
            return _token_cache[0]
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
            expires_in = float(payload.get("expires_in", 600))
        except (ValueError, KeyError, TypeError):
            raise ValidationError(
                "could not read the Digi-Key token response"
            ) from None
        _token_cache = (token, now + expires_in)
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
        if not (config.DIGIKEY_CLIENT_ID and config.DIGIKEY_CLIENT_SECRET):
            raise ValidationError("Digi-Key integration is not configured")
        part_number = _part_number(url)

        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:
                token = _access_token(client)
                resp = client.get(
                    f"{config.DIGIKEY_API_BASE}/products/v4/search/"
                    f"{quote(part_number, safe='')}/productdetails",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-DIGIKEY-Client-Id": config.DIGIKEY_CLIENT_ID,
                        "X-DIGIKEY-Locale-Site": config.DIGIKEY_LOCALE_SITE,
                        "X-DIGIKEY-Locale-Language": config.DIGIKEY_LOCALE_LANGUAGE,
                        "X-DIGIKEY-Locale-Currency": config.DIGIKEY_LOCALE_CURRENCY,
                    },
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
            raise ValidationError("no product found for this URL")

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
            mpn=product.get("ManufacturerProductNumber") or None,
            manufacturer=_nested("Manufacturer", "Name"),
            description=description,
            datasheet_url=product.get("DatasheetUrl") or None,
            category=infer_category(category, description),
            shop_category=category,
            parameters=parameters,
        )

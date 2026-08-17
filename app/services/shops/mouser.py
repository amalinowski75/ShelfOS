"""Mouser Search API provider (spec: create a component from a shop URL)."""

from __future__ import annotations

import logging
from urllib.parse import quote, unquote, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import (
    ProductData,
    ShopLookupMiss,
    fetch_first_match,
    infer_category,
)

_API_URL = "https://api.mouser.com/api/v1/search/partnumber"
_logger = logging.getLogger("shelfos")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:  # e.g. an unbalanced-bracket IPv6 literal
        return ""


def _redact(text: str) -> str:
    """Never let the API key ride out in a message, however unlikely."""
    key = config.MOUSER_API_KEY
    return text.replace(key, "***") if key else text


def _error_text(errors: object) -> str:
    """Mouser's own error text (e.g. "Invalid unique identifier." for a bad key).

    Surfaced to the user: it's the shop's message, not a secret, and without it a
    misconfigured key is undiagnosable.
    """
    messages: list[str] = []
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict):
                message = err.get("Message") or err.get("message")
                if message:
                    messages.append(str(message))
    return _redact("; ".join(messages)) or "unknown error"


class MouserProvider:
    name = "Mouser"

    def matches(self, url: str) -> bool:
        # Mouser runs many country sites (mouser.pl, mouser.de, mouser.co.uk,
        # eu.mouser.com …), so match "mouser" as a domain label rather than one
        # fixed host. A false match (e.g. mouser.example.com) is harmless: the
        # lookup still only ever calls the fixed api.mouser.com and would just
        # report "no product found".
        labels = _host(url).split(".")
        return len(labels) > 1 and "mouser" in labels[:-1]

    def product_url(self, part_number: str) -> str | None:
        # No stable direct-product path from a part number alone, so link the
        # keyword search — an exact Mouser number lands on the single product.
        part = part_number.strip()
        return f"https://www.mouser.com/c/?q={quote(part)}" if part else None

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        # The product number is the last path segment of a Mouser product URL; the
        # API call itself is URL-independent, so a scan reuses fetch_by_mpn.
        try:
            path = urlsplit(url).path
        except ValueError:
            raise ValidationError("malformed URL") from None
        part_number = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if not part_number:
            raise ValidationError("could not read a part number from the URL")
        return self.fetch_by_mpn(part_number, transport=transport)

    def fetch_by_index(
        self, candidates: list[str], *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        """Try each candidate number in order; first hit wins (invoice import).

        The invoice's own Mouser number ("771-NX3P1108UKZ") comes first — it is the
        canonical key and ``mouserPartNumber`` matches Mouser's own SKU directly —
        with the parsed MPN as the fallback (see ``fetch_first_match`` for the
        miss-only fallthrough and error aggregation).
        """
        return fetch_first_match(
            lambda number: self.fetch_by_mpn(number, transport=transport),
            candidates,
        )

    def fetch_by_mpn(
        self, mpn: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        """Look a part up by its number directly (from a scan, not a URL)."""
        if not config.MOUSER_API_KEY:
            raise ValidationError("Mouser integration is not configured")
        # partSearchOptions is optional; omit it rather than risk an invalid value.
        # mouserPartNumber matches on a manufacturer PN or Mouser's own SKU.
        body = {"SearchByPartRequest": {"mouserPartNumber": mpn}}
        # A network error, a non-2xx, a non-JSON body (JSONDecodeError → ValueError)
        # or an unexpected JSON shape (AttributeError) all become a clean 422 — never
        # a 500, and the exception (which could embed the api-key query string) never
        # reaches the client.
        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:
                resp = client.post(
                    _API_URL, params={"apiKey": config.MOUSER_API_KEY}, json=body
                )
                resp.raise_for_status()
                payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("unexpected response shape")
            errors = payload.get("Errors")
            parts = (payload.get("SearchResults") or {}).get("Parts") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            raise ValidationError("could not read the Mouser response") from None

        if errors:
            detail = _error_text(errors)
            # Logged too: a bad/wrong-type key ("Invalid unique identifier.") is the
            # usual cause, and Mouser issues separate Search and Order API keys.
            _logger.warning("Mouser lookup failed: %s", detail)
            raise ValidationError(f"Mouser rejected the request: {detail}")
        if not parts:
            raise ShopLookupMiss("no product found")
        # SearchByPartRequest matches PARTIALLY (a queried number that is a prefix of
        # a reel variant returns the variant), so never take parts[0] on faith: accept
        # only a part whose Mouser or manufacturer number equals what was asked for.
        # Anything else is a miss — the next candidate (or review) beats silently
        # importing a different part's identity.
        queried = mpn.strip().casefold()
        part = next(
            (
                p
                for p in parts
                if isinstance(p, dict)
                and queried
                in (
                    str(p.get("MouserPartNumber") or "").strip().casefold(),
                    str(p.get("ManufacturerPartNumber") or "").strip().casefold(),
                )
            ),
            None,
        )
        if part is None:
            raise ShopLookupMiss(f"no exact match for {mpn!r}")

        parameters: list[tuple[str, str]] = []
        for attr in part.get("ProductAttributes") or []:
            if not isinstance(attr, dict):
                continue
            name = (attr.get("AttributeName") or "").strip()
            value = (attr.get("AttributeValue") or "").strip()
            if name and value:
                parameters.append((name, value))  # raw; cleaned client-side per type

        return ProductData(
            # The product page from the response, not from the input: a scan looks a
            # part up by number and has no URL of its own, and this is what gets kept
            # as the component's shop link.
            source_url=part.get("ProductDetailUrl") or None,
            mpn=part.get("ManufacturerPartNumber") or None,
            manufacturer=part.get("Manufacturer") or None,
            description=part.get("Description") or None,
            datasheet_url=part.get("DataSheetUrl") or None,
            category=infer_category(part.get("Category"), part.get("Description")),
            shop_category=part.get("Category") or None,
            parameters=parameters,
        )

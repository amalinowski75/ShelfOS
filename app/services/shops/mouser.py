"""Mouser Search API provider (spec: create a component from a shop URL)."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import ProductData, infer_category

_API_URL = "https://api.mouser.com/api/v1/search/partnumber"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:  # e.g. an unbalanced-bracket IPv6 literal
        return ""


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

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        if not config.MOUSER_API_KEY:
            raise ValidationError("Mouser integration is not configured")
        # The product number is the last path segment of a Mouser product URL.
        try:
            path = urlsplit(url).path
        except ValueError:
            raise ValidationError("malformed URL") from None
        part_number = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if not part_number:
            raise ValidationError("could not read a part number from the URL")

        body = {
            "SearchByPartRequest": {
                "mouserPartNumber": part_number,
                "partSearchOptions": "1",
            }
        }
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
            raise ValidationError("Mouser returned an error for this URL")
        if not parts:
            raise ValidationError("no product found for this URL")
        part = parts[0]
        if not isinstance(part, dict):
            raise ValidationError("could not read the Mouser response")

        parameters: list[tuple[str, str]] = []
        for attr in part.get("ProductAttributes") or []:
            if not isinstance(attr, dict):
                continue
            name = (attr.get("AttributeName") or "").strip()
            value = (attr.get("AttributeValue") or "").strip()
            if name and value:
                parameters.append((name, value))  # raw; cleaned client-side per type

        return ProductData(
            mpn=part.get("ManufacturerPartNumber") or None,
            manufacturer=part.get("Manufacturer") or None,
            description=part.get("Description") or None,
            datasheet_url=part.get("DataSheetUrl") or None,
            category=infer_category(part.get("Category"), part.get("Description")),
            parameters=parameters,
        )

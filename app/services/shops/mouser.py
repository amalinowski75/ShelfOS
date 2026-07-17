"""Mouser Search API provider (spec: create a component from a shop URL)."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import ProductData, clean_param_value, infer_category

_API_URL = "https://api.mouser.com/api/v1/search/partnumber"


class MouserProvider:
    name = "Mouser"

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host == "mouser.com" or host.endswith(".mouser.com")

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        if not config.MOUSER_API_KEY:
            raise ValidationError("Mouser integration is not configured")
        # The product number is the last path segment of a Mouser product URL.
        part_number = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        if not part_number:
            raise ValidationError("could not read a part number from the URL")

        body = {
            "SearchByPartRequest": {
                "mouserPartNumber": part_number,
                "partSearchOptions": "1",
            }
        }
        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:
                resp = client.post(
                    _API_URL, params={"apiKey": config.MOUSER_API_KEY}, json=body
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError:
            raise ValidationError("could not reach Mouser") from None

        if payload.get("Errors"):
            raise ValidationError("Mouser returned an error for this URL")
        parts = (payload.get("SearchResults") or {}).get("Parts") or []
        if not parts:
            raise ValidationError("no product found for this URL")
        part = parts[0]

        parameters: list[tuple[str, str]] = []
        for attr in part.get("ProductAttributes") or []:
            name = (attr.get("AttributeName") or "").strip()
            value = attr.get("AttributeValue") or ""
            if name and value:
                parameters.append((name, clean_param_value(value)))

        return ProductData(
            mpn=part.get("ManufacturerPartNumber") or None,
            manufacturer=part.get("Manufacturer") or None,
            description=part.get("Description") or None,
            datasheet_url=part.get("DataSheetUrl") or None,
            category=infer_category(part.get("Category"), part.get("Description")),
            parameters=parameters,
        )

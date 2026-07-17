"""Shop-provider registry (spec: create a component from a shop URL).

Adding a distributor = a new module implementing ``ShopProvider`` + one entry in
``_PROVIDERS``. ``lookup`` dispatches by URL host.
"""

from __future__ import annotations

from app.services.errors import ValidationError
from app.services.shops.base import ProductData, ShopProvider
from app.services.shops.mouser import MouserProvider

_PROVIDERS: list[ShopProvider] = [MouserProvider()]


def resolve(url: str) -> ShopProvider | None:
    """The provider whose host matches ``url``, or None."""
    for provider in _PROVIDERS:
        if provider.matches(url):
            return provider
    return None


def lookup(url: str) -> ProductData:
    """Look a product up via its shop's API. Raises ValidationError on failure."""
    provider = resolve(url)
    if provider is None:
        raise ValidationError("unsupported shop — no provider for this URL")
    return provider.fetch(url)


__all__ = ["ProductData", "ShopProvider", "lookup", "resolve"]

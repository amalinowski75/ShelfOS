"""Shop-provider registry (spec: create a component from a shop URL).

Adding a distributor = a new module implementing ``ShopProvider`` + one entry in
``_PROVIDERS``. ``lookup`` dispatches by URL host; ``import_code`` is the entry the
dialog uses and additionally accepts a scanned barcode (see ``scan``).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.services.errors import ValidationError
from app.services.shops.base import ProductData, ShopProvider
from app.services.shops.digikey import DigiKeyProvider
from app.services.shops.mouser import MouserProvider
from app.services.shops.scan import ScanResult, parse_scan
from app.services.shops.tme import TmeProvider

_logger = logging.getLogger("shelfos")


@runtime_checkable
class MpnProvider(Protocol):
    """A provider that can also look a part up by its part number alone."""

    def fetch_by_mpn(self, mpn: str) -> ProductData: ...


_mouser = MouserProvider()
_digikey = DigiKeyProvider()

_PROVIDERS: list[ShopProvider] = [_mouser, _digikey, TmeProvider()]

# Shops whose DataMatrix label we can look up by part number alone. TME is absent
# on purpose: its API keys on TME's own symbol, not the MPN — and a scanned TME QR
# carries a product URL anyway, so it takes the URL path.
_BY_MPN: dict[str, MpnProvider] = {"mouser": _mouser, "digikey": _digikey}


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


def _fallback(scan: ScanResult, error: ValidationError | None) -> ProductData:
    """What the label itself told us, when the shop's API couldn't add to it.

    A scan that parsed usually yields at least an MPN, so an unconfigured or failing
    shop still prefills the dialog instead of dead-ending; the user fills the rest in
    by hand. With no MPN there is nothing to offer, so the API's own error is the
    more useful thing to surface.

    The swallowed error is logged: it's routinely "<shop> integration is not
    configured", and silently degrading every scan is how a missing key stays
    unnoticed for months. ``from_label_only`` carries the same fact to the dialog.
    """
    if not scan.mpn:
        raise error or ValidationError("could not read a part number from the code")
    if error is not None:
        _logger.warning("shop lookup failed, filling from the label: %s", error)
    return ProductData(
        mpn=scan.mpn,
        manufacturer=scan.manufacturer,
        source_url=scan.url,
        from_label_only=True,
    )


def import_code(code: str) -> ProductData:
    """Look a product up from a shop URL *or* a scanned barcode/QR.

    Raises ValidationError if the code can't be parsed, or if it yields neither a
    usable lookup nor an MPN to fall back on.
    """
    scan = parse_scan(code)

    if scan.url:
        provider = resolve(scan.url)
        if provider is None:
            # A URL we can't look up is still worth something if the code also
            # carried a part number (a TME QR does); otherwise say so plainly.
            if not scan.mpn:
                raise ValidationError("unsupported shop — no provider for this URL")
            return _fallback(scan, None)
        try:
            product = provider.fetch(scan.url)
        except ValidationError as exc:
            return _fallback(scan, exc)
        product.source_url = scan.url
        return product

    # A DataMatrix: no URL, but the label named its shop and part number.
    provider_by_mpn = _BY_MPN.get(scan.shop or "")
    if provider_by_mpn is None or not scan.mpn:
        return _fallback(scan, None)  # raises if there was no MPN either
    try:
        return provider_by_mpn.fetch_by_mpn(scan.mpn)
    except ValidationError as exc:
        return _fallback(scan, exc)


__all__ = ["ProductData", "ShopProvider", "import_code", "lookup", "resolve"]

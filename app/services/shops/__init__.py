"""Shop-provider registry (spec: create a component from a shop URL).

Adding a distributor = a new module implementing ``ShopProvider`` + one entry in
``_PROVIDERS``. ``import_code`` is the single entry point: it takes a shop URL or a
scanned barcode (see ``scan``) and dispatches by URL host or by the shop the label
names.
"""

from __future__ import annotations

import logging
from typing import Protocol, cast

from app.services.errors import ValidationError
from app.services.shops.base import ProductData, ShopProvider
from app.services.shops.digikey import DigiKeyProvider
from app.services.shops.farnell import FarnellProvider
from app.services.shops.mouser import MouserProvider
from app.services.shops.scan import ScanResult, parse_scan
from app.services.shops.tme import TmeProvider

_logger = logging.getLogger("shelfos")


class MpnProvider(Protocol):
    """A provider that can also look a part up by its part number alone.

    ``manufacturer`` (a scan's ``1V`` field, an invoice line's) disambiguates a bare
    MPN sold under several makers; a provider that resolves one product per number
    accepts and ignores it, for a uniform interface.
    """

    def fetch_by_mpn(
        self, mpn: str, *, manufacturer: str | None = None
    ) -> ProductData: ...


class IndexProvider(Protocol):
    """A provider that can look a part up by the shop's own catalogue index.

    ``candidates`` are tried best-first (the invoice's shop index, then the parsed
    MPN); how they are consumed is the provider's business — Mouser/Digi-Key try
    them one call at a time, TME offers them all in a single call. ``manufacturer``
    (the invoice line's) breaks ties when a bare MPN maps to several makers.
    """

    def fetch_by_index(
        self, candidates: list[str], *, manufacturer: str | None = None
    ) -> ProductData: ...


_mouser = MouserProvider()
_digikey = DigiKeyProvider()
_tme = TmeProvider()
_farnell = FarnellProvider()

_PROVIDERS: list[ShopProvider] = [_mouser, _digikey, _tme, _farnell]

# Shops whose DataMatrix label we can look up by part number alone. TME is absent
# on purpose: its API keys on TME's own symbol, not the MPN — and a scanned TME QR
# carries a product URL anyway, so it takes the URL path.
#
# Farnell reaches this map through the 3P field on its DataMatrix (see `scan`),
# which is the one identifier neither of the others prints. Note what that leaves
# on the table: 3P carries element14's ORDER CODE, the key their API resolves with
# `id:` to exactly one product — and `import_code` still looks a scanned label up by
# its MPN, so Farnell's canonical-variant rule picks the packaging variant rather
# than the bag in your hand. Routing scans through `fetch_by_index` instead would
# fix that, but it would also change Digi-Key's path (its labels carry a distributor
# number too), which is not verifiable without a key.
_BY_MPN: dict[str, MpnProvider] = {
    "mouser": _mouser,
    "digikey": _digikey,
    "farnell": _farnell,
}

# Invoice-import enrichment, keyed by the shop's own catalogue index (the invoice
# always carries it in the item row itself). Including TME, whose symbol is exactly
# that index, giving it API enrichment the MPN path never could. This map is also
# what `product_url` dispatches on, so a shop missing here has a permanently greyed
# "open in shop" button.
#
# Farnell's enrichment is the sharpest of the four: its invoice prints element14's
# own order code in the item row, and that is exactly the key its API resolves with
# `id:` — one product, no maker to disambiguate. (Its _BY_MPN entry above is still
# unreachable; only the invoice names the shop, a scan does not.)
_BY_INDEX: dict[str, IndexProvider] = {
    "mouser": _mouser,
    "digikey": _digikey,
    "tme": _tme,
    "farnell": _farnell,
}


def product_url(shop_key: str | None, part_number: str | None) -> str | None:
    """The shop's public product page for a supplier part number, or None.

    Used to link a component created from an invoice line back to its distributor:
    the line carries the shop and its own part number, which each provider turns
    into a URL without any API call. Returns None for an unknown shop or a blank
    number (the caller then leaves the "open in shop" button disabled).

    Keyed off the same ``_BY_INDEX`` map the invoice import already dispatches on,
    so the shop set can't silently drift between the two — a shop missing here would
    otherwise be an unexplained permanently-greyed button. The blank check lives
    here, so providers receive a clean, non-empty number.
    """
    part = (part_number or "").strip()
    provider = _BY_INDEX.get((shop_key or "").lower())
    if provider is None or not part:
        return None
    return cast(ShopProvider, provider).product_url(part)


def import_by_index(
    shop_key: str | None,
    candidates: list[str],
    *,
    manufacturer: str | None = None,
) -> ProductData:
    """Look a product up by the shop's OWN catalogue index, best candidate first.

    The lookup an invoice line needs. Its ``product_url`` is not usable for this:
    only TME's round-trips through ``fetch`` — every other provider links a keyword
    SEARCH (no stable product path exists from a part number alone), whose URL
    carries no part number a ``fetch`` could read, so looking one up would answer
    with whatever the shop makes of "result"/"search". Keyed on the shop and the
    numbers themselves, this is the same call ``invoice_import_service._enrich``
    makes, and it answers for the part the line actually is.

    Raises ValidationError for an unknown shop or no usable number.
    """
    provider = _BY_INDEX.get((shop_key or "").lower())
    if provider is None:
        raise ValidationError("unsupported shop — no provider for this line")
    numbers = [n for n in dict.fromkeys(c.strip() for c in candidates) if n]
    if not numbers:
        raise ValidationError("no part number to look this line up by")
    return provider.fetch_by_index(numbers, manufacturer=manufacturer)


def resolve(url: str) -> ShopProvider | None:
    """The provider whose host matches ``url``, or None."""
    for provider in _PROVIDERS:
        if provider.matches(url):
            return provider
    return None


def _fallback(
    scan: ScanResult, error: ValidationError | None, *, keep_url: bool = True
) -> ProductData:
    """What the label itself told us, when the shop's API couldn't add to it.

    A scan that parsed usually yields at least an MPN, so an unconfigured or failing
    shop still prefills the dialog instead of dead-ending; the user fills the rest in
    by hand. With no MPN there is nothing to offer, so the API's own error is the
    more useful thing to surface.

    The swallowed error is logged: it's routinely "<shop> integration is not
    configured", and silently degrading every scan is how a missing key stays
    unnoticed for months. ``from_label_only`` carries the same fact to the dialog.

    ``keep_url=False`` for a URL no provider matched: the client stores ``source_url``
    as the component's *shop* link, and a page we just said we can't interpret hasn't
    earned that label.
    """
    if not scan.mpn:
        raise error or ValidationError("could not read a part number from the code")
    if error is not None:
        _logger.warning("shop lookup failed, filling from the label: %s", error)
    return ProductData(
        mpn=scan.mpn,
        manufacturer=scan.manufacturer,
        source_url=scan.url if keep_url else None,
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
            return _fallback(scan, None, keep_url=False)
        try:
            product = provider.fetch(scan.url)
        except ValidationError as exc:
            return _fallback(scan, exc)
        # The URL the user actually pasted/scanned wins over the canonical one from
        # the response: it's the page they were looking at.
        product.source_url = scan.url
        return product

    # A DataMatrix: no URL, but the label named its shop and part number.
    provider_by_mpn = _BY_MPN.get(scan.shop or "")
    if provider_by_mpn is None or not scan.mpn:
        return _fallback(scan, None)  # raises if there was no MPN either
    try:
        # The label's 1V manufacturer disambiguates a bare MPN that several makers
        # share ("5120" is Keystone's and ABB's) — otherwise the shop's own ordering
        # would pick, and the wrong company could win.
        return provider_by_mpn.fetch_by_mpn(scan.mpn, manufacturer=scan.manufacturer)
    except ValidationError as exc:
        return _fallback(scan, exc)


__all__ = ["ProductData", "ShopProvider", "import_code", "product_url", "resolve"]

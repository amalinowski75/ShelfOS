"""Shop-integration endpoints (spec: create a component from a shop URL).

Thin: the provider registry does the work. Mounted under the protected routers,
so POST requires a writer + CSRF (read-only accounts can't create components, and
a lookup spends the shop's API quota).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ScanParseRead, ShopLookup, ShopParameter, ShopProductRead
from app.services import shops
from app.services.shops import tme
from app.services.shops.scan import parse_scan

router = APIRouter(prefix="/api/shops", tags=["shops"])


def _url_symbols(url: str | None) -> list[str]:
    """The shop's own symbols embedded in a scanned product URL.

    A TME QR sometimes carries only the URL, whose path holds TME's symbol —
    the value an invoice line stores as ``supplier_part_number``, and so the
    only thing such a scan can be matched by. The symbol's position in the
    path is not fixed, so every plausible segment is returned rather than
    guessed between (the same list ``fetch_by_symbols`` offers the API).
    """
    if not url:
        return []
    provider = shops.resolve(url)
    if getattr(provider, "name", None) != tme.TmeProvider.name:
        return []
    return tme.url_symbols(url)


@router.post("/parse", response_model=ScanParseRead)
def parse_code(payload: ShopLookup) -> ScanParseRead:
    """Decode a scanned label to its identifiers without calling any shop API.

    For flows that match the scan against data already at hand (a draft
    invoice's lines), where a full lookup would spend API quota for nothing.
    """
    result = parse_scan(payload.code)  # ValidationError → 422
    return ScanParseRead(
        mpn=result.mpn,
        manufacturer=result.manufacturer,
        distributor_pn=result.distributor_pn,
        shop=result.shop,
        url=result.url,
        url_symbols=_url_symbols(result.url),
    )


@router.post("/lookup", response_model=ShopProductRead)
def lookup_product(payload: ShopLookup) -> ShopProductRead:
    """Look a product up from a URL or a scanned code, normalised for the dialog."""
    product = shops.import_code(payload.code)  # ValidationError → 422; key never leaks
    return ShopProductRead(
        category=product.category,
        shop_category=product.shop_category,
        mpn=product.mpn,
        manufacturer=product.manufacturer,
        description=product.description,
        package=product.package,
        datasheet_url=product.datasheet_url,
        parameters=[ShopParameter(name=n, value=v) for n, v in product.parameters],
        source_url=product.source_url,
        from_label_only=product.from_label_only,
    )

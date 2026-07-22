"""Shop-integration endpoints (spec: create a component from a shop URL).

Thin: the provider registry does the work. Mounted under the protected routers,
so POST requires a writer + CSRF (read-only accounts can't create components, and
a lookup spends the shop's API quota).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ShopLookup, ShopParameter, ShopProductRead
from app.services import shops

router = APIRouter(prefix="/api/shops", tags=["shops"])


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

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
    """Look a product up via its shop's API and normalise it for the dialog."""
    product = shops.lookup(payload.url)  # ValidationError → 422; key never leaks
    return ShopProductRead(
        category=product.category,
        mpn=product.mpn,
        manufacturer=product.manufacturer,
        description=product.description,
        package=product.package,
        datasheet_url=product.datasheet_url,
        parameters=[ShopParameter(name=n, value=v) for n, v in product.parameters],
    )

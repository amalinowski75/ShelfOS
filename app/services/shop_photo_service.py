"""A component's product photo, from the shop it was imported from or another one.

A shop import names an image, but not every shop lets a server download it:
Mouser's Akamai answers every script — the hosted server and a laptop alike —
with an HTML block page, which ``url_fetch`` refuses. So a part bought there used
to arrive without a picture. The same part is usually sold by Digi-Key or
Farnell too, whose APIs look a part up by its MPN and whose images download, so
when the imported image cannot be had, theirs is used instead.

The fallback answer must be THIS part. Its MPN has to match the component's,
and its maker must not plainly be another company
(:func:`manufacturer_service.conflicts_with`): one number is sold under several
makers, even within a single shop — Farnell answers a Keystone "5270" with
ABB's.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.models.attachment import Attachment
from app.models.component import Component
from app.models.enums import AttachmentKind
from app.services import attachment_service, shops, url_fetch
from app.services import manufacturer_service as ms
from app.services._common import normalize, require_entity
from app.services.errors import ValidationError

log = logging.getLogger(__name__)

# Shops asked by part number once the imported image has failed, in order. Only
# shops whose API takes an MPN: TME's takes its own catalogue symbol instead.
FALLBACK_SHOPS = ("digikey", "farnell")


def attach_photo(
    session: Session, component_id: int, image_url: str | None
) -> Attachment:
    """Attach a product photo to the component, from ``image_url`` or a fallback.

    ``image_url`` is what the import's own shop offered, tried first; it may be
    absent, since some shops have no picture for a part another one does. Then
    each fallback shop is asked by the component's MPN, and its image used if the
    answer is this part. The first image that downloads is attached.

    Raises ValidationError, naming what each source said, when none did — a
    component with no MPN gets only ``image_url``, having nothing to check a
    fallback's answer against.
    """
    component = require_entity(session, Component, component_id, "component")
    tried: list[str] = []
    reasons: list[str] = []

    def attempt(url: str, source: str) -> Attachment | None:
        tried.append(url)
        try:
            data, filename = url_fetch.fetch_url(url)
        except ValidationError as exc:
            reasons.append(f"{source}: {exc}")
            return None
        return attachment_service.create_attachment(
            session,
            entity_type="component",
            entity_id=component_id,
            kind=AttachmentKind.PHOTO,
            filename=filename,
            data=data,
        )

    if image_url:
        attached = attempt(image_url, "the shop's image")
        if attached is not None:
            return attached

    if component.mpn:
        for shop in FALLBACK_SHOPS:
            url, reason = _fallback_image(session, component, shop)
            if url is None:
                reasons.append(f"{shop}: {reason}")
                continue
            if url in tried:  # the same picture, refused once already
                continue
            attached = attempt(url, shop)
            if attached is not None:
                return attached
    elif not image_url:
        raise ValidationError("no image, and no part number to look one up by")

    raise ValidationError("no photo could be downloaded — " + "; ".join(reasons))


def _fallback_image(
    session: Session, component: Component, shop: str
) -> tuple[str | None, str]:
    """``(image URL, "")`` when ``shop`` sells this very part, else ``(None, why)``."""
    try:
        product = shops.import_by_index(
            shop, [component.mpn or ""], manufacturer=component.manufacturer
        )
    except ValidationError as exc:
        # Not configured, a miss, the API down: this shop simply has nothing to
        # offer, and the next one may.
        return None, str(exc)
    if normalize(product.mpn) != normalize(component.mpn):
        return None, f"answered with {product.mpn or 'an unnamed part'}"
    if ms.conflicts_with(
        ms.canonical_name(session, product.manufacturer),
        ms.canonical_name(session, component.manufacturer),
    ):
        return None, f"answered with {product.manufacturer}'s part"
    if not product.image_url:
        return None, "no image for this part"
    log.info("photo for component %s: falling back to %s", component.id, shop)
    return product.image_url, ""

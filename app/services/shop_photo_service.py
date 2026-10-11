"""A component's product photo, from the shop it was imported from or another one.

A shop import names an image, but not every shop lets a server download it:
Mouser's Akamai answers every script — the hosted server and a laptop alike —
with an HTML block page, which ``url_fetch`` refuses. So a part bought there used
to arrive without a picture. The same part is usually sold by Digi-Key or
element14 too, whose APIs look a part up by its MPN and whose images download, so
when the imported image cannot be had, theirs is used instead.

A fallback answer must be THIS part. Its MPN has to match the component's, and
both sides have to name a maker that :func:`manufacturer_service.conflicts_with`
finds no conflict between: one number is sold under several makers, even within
a single shop — element14 answers a Keystone "5270" with ABB's. A component that
names no maker has nothing to tell those apart by, so it gets only the image it
was given.

The whole chain shares one time budget, the single download's own ceiling, so a
dialog waiting on it waits no longer than it did for one download (plus at most
one shop lookup already under way).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlmodel import Session

from app import config
from app.models.attachment import Attachment
from app.models.component import Component
from app.models.enums import AttachmentKind
from app.services import attachment_service, shops, url_fetch
from app.services import manufacturer_service as ms
from app.services._common import normalize, refuse_if_deleted, require_entity
from app.services.errors import ValidationError

# Shops asked by part number once the imported image has failed, in order. Only
# shops whose API takes an MPN: TME's takes its own catalogue symbol instead.
FALLBACK_SHOPS = ("digikey", "farnell")


def attach_photo(
    session: Session,
    component_id: int,
    image_url: str | None,
    *,
    source_url: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Attachment, bool]:
    """Give the component a product photo; ``(attachment, created)``.

    ``image_url`` is what the import's own shop offered, tried first; it may be
    absent, since a shop with no picture of a part may sell it all the same.
    Then each fallback shop is asked by the component's MPN — except the one
    ``source_url`` came from, which would only offer the same picture again —
    and its image used if the answer is this part.

    A component that already has a photo gets it back with ``created`` False,
    and nothing is fetched: a retried or doubled request must not stack copies.

    Raises ValidationError, naming what each source said, when nothing could be
    had — and before any shop is asked, for a deleted component.
    """
    component = require_entity(session, Component, component_id, "component")
    refuse_if_deleted(component, component.mpn or f"component #{component_id}")
    existing = next(
        (
            a
            for a in attachment_service.list_attachments(
                session, entity_type="component", entity_id=component_id
            )
            if a.kind is AttachmentKind.PHOTO
        ),
        None,
    )
    if existing is not None:
        return existing, False

    deadline = clock() + config.ATTACHMENT_URL_TOTAL_TIMEOUT
    tried: list[str] = []
    reasons: list[str] = []

    def attempt(url: str, source: str) -> Attachment | None:
        tried.append(url)
        try:
            data, filename = url_fetch.fetch_url(url, deadline=deadline)
            # Inside the try too: an empty or oversize file is this source's
            # failure, and the next one may still have a usable picture.
            return attachment_service.create_attachment(
                session,
                entity_type="component",
                entity_id=component_id,
                kind=AttachmentKind.PHOTO,
                filename=filename,
                data=data,
            )
        except ValidationError as exc:
            reasons.append(f"{source}: {exc}")
            return None

    if image_url:
        attached = attempt(image_url, "the shop's image")
        if attached is not None:
            return attached, True

    if not (component.mpn and component.manufacturer):
        what = "part number" if not component.mpn else "manufacturer"
        reasons.append(f"no {what} to check another shop's answer against")
    else:
        source = shops.shop_key(source_url)
        for shop in FALLBACK_SHOPS:
            if shop == source:
                continue
            # A lookup is not cut short by the deadline, so one is started only
            # while it can finish inside it.
            if deadline - clock() < config.SHOP_API_TIMEOUT:
                reasons.append(f"{shop}: not asked, out of time")
                continue
            url, reason = _fallback_image(session, component, shop)
            if url is None:
                reasons.append(f"{shop}: {reason}")
                continue
            if url in tried:  # the same picture, refused once already
                continue
            attached = attempt(url, shop)
            if attached is not None:
                return attached, True

    raise ValidationError("no photo could be downloaded — " + "; ".join(reasons))


def _fallback_image(
    session: Session, component: Component, shop: str
) -> tuple[str | None, str]:
    """``(image URL, "")`` when ``shop`` sells this very part, else ``(None, why)``."""
    try:
        product = shops.import_by_mpn(
            shop, component.mpn or "", manufacturer=component.manufacturer
        )
    except ValidationError as exc:
        # Not configured, a miss, the API down: this shop simply has nothing to
        # offer, and the next one may.
        return None, str(exc)
    if normalize(product.mpn) != normalize(component.mpn):
        return None, f"answered with {product.mpn or 'an unnamed part'}"
    # False, not merely "not True": a shop answer naming no maker cannot show
    # it is not another company's part.
    if (
        ms.conflicts_with(
            ms.canonical_name(session, product.manufacturer),
            ms.canonical_name(session, component.manufacturer),
        )
        is not False
    ):
        return None, f"answered with {product.manufacturer or 'an unnamed'}'s part"
    if not product.image_url:
        return None, "no image for this part"
    return product.image_url, ""

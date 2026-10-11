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
from app.models.invoice import Invoice, InvoiceLine
from app.services import attachment_service, shops, url_fetch
from app.services import manufacturer_service as ms
from app.services._common import normalize, refuse_if_deleted, require_entity
from app.services.errors import NotFoundError, ValidationError

# Shops asked by part number once the imported image has failed, in order. Only
# shops whose API takes an MPN: TME's takes its own catalogue symbol instead.
FALLBACK_SHOPS = ("digikey", "farnell")


def attach_photo(
    session: Session,
    component_id: int,
    image_url: str | None,
    *,
    source_url: str | None = None,
    lookup: tuple[str, list[str]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Attachment, bool]:
    """Give the component a product photo; ``(attachment, created)``.

    ``image_url`` is what the import's own shop offered, tried first; it may be
    absent, since a shop with no picture of a part may sell it all the same.
    Then each fallback shop is asked by the component's MPN — except the one
    ``source_url`` came from, which would only offer the same picture again —
    and its image used if the answer is this part.

    ``lookup`` — ``(shop key, part numbers)``, best first — is for a caller that
    knows the shop the part came from but not its image, such as an invoice line:
    with no ``image_url``, that shop is asked by its own catalogue index first
    (the lookup the invoice import makes) and its image tried, then the fallbacks.

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

    source = shops.shop_key(source_url)
    if not image_url and lookup is not None:
        source, numbers = lookup
        image_url, reason = _own_shop_image(session, component, source, numbers)
        if image_url is None:
            reasons.append(f"{source}: {reason}")

    if image_url:
        attached = attempt(image_url, "the shop's image")
        if attached is not None:
            return attached, True

    if not (component.mpn and component.manufacturer):
        what = "part number" if not component.mpn else "manufacturer"
        reasons.append(f"no {what} to check another shop's answer against")
    else:
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


def attach_invoice_line_photo(
    session: Session, invoice_id: int, line_id: int
) -> tuple[Attachment, bool]:
    """Give an invoice line's component a photo, asking the invoice's shop first.

    For the invoice page right after finalize: an invoice keeps no image URL —
    the import's shop answer is not stored — so the shop is asked again, by the
    line's own catalogue number (the key the import looked it up by) and then the
    component's MPN. A supplier no provider serves (a hand-made invoice) goes
    straight to the fallbacks. A component that already has a photo is returned
    as it is, with no shop asked, so every line can simply be offered.
    """
    line = session.get(InvoiceLine, line_id)
    if line is None or line.invoice_id != invoice_id:
        raise NotFoundError(f"invoice line {line_id} not found")
    invoice = require_entity(session, Invoice, invoice_id, "invoice")
    component = require_entity(session, Component, line.component_id, "component")
    shop = shops.shop_key_by_name(invoice.supplier)
    numbers = [
        n for n in dict.fromkeys((line.supplier_part_number, component.mpn)) if n
    ]
    return attach_photo(
        session,
        line.component_id,
        None,
        lookup=(shop, numbers) if shop and numbers else None,
    )


def _own_shop_image(
    session: Session, component: Component, shop: str, numbers: list[str]
) -> tuple[str | None, str]:
    """The image the part's own shop shows for it, by that shop's catalogue index.

    Checked more leniently than a fallback's answer: the part was bought there,
    under that very index, so a maker named on one side only does not refuse it,
    and an answer naming no MPN (TME may not) is taken on the index alone. A
    stated different MPN or a plainly different maker still does.
    """
    try:
        product = shops.import_by_index(
            shop, numbers, manufacturer=component.manufacturer
        )
    except ValidationError as exc:
        return None, str(exc)
    if (
        product.mpn
        and component.mpn
        and normalize(product.mpn) != normalize(component.mpn)
    ):
        return None, f"answered with {product.mpn}"
    if ms.conflicts_with(
        ms.canonical_name(session, product.manufacturer),
        ms.canonical_name(session, component.manufacturer),
    ):
        return None, f"answered with {product.manufacturer}'s part"
    if not product.image_url:
        return None, "no image for this part"
    return product.image_url, ""


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

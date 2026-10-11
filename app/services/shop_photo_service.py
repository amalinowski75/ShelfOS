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

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast

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
from app.services.shops.base import ProductData, ShopLookupMiss

# Shops asked by part number once the imported image has failed, in order. Only
# shops whose API takes an MPN: TME's takes its own catalogue symbol instead.
FALLBACK_SHOPS = ("digikey", "farnell")

# A shop that could not answer at all — not configured, unreachable, an
# unreadable answer, a refusal with a failing status (Mouser's spent quota) — is
# not asked again for this long, by any request. The invoice page asks once per
# line, one request each; without this a 12-line invoice with TME down would wait
# out 12 TME timeouts. A miss, or a refusal of one part (Digi-Key's "Duplicate
# Products found"), says nothing about the next part, and trips nothing.
SHOP_DOWN_SECONDS = 120.0
_UNAVAILABLE = re.compile(
    r"integration is not configured|could not reach |could not read the "
    r"|\(HTTP \d+\)",
    re.IGNORECASE,
)
_down: dict[str, tuple[float, str]] = {}
_down_lock = threading.Lock()

# One request at a time per component, so two that race (an invoice finalized in
# two tabs, a retry while the first is still downloading) cannot both find no
# photo and both attach one: the second waits, then finds the first's. Per
# process, which is what ShelfOS runs as.
_component_locks: dict[int, threading.Lock] = {}
_component_locks_guard = threading.Lock()


@contextmanager
def _one_at_a_time(component_id: int) -> Iterator[None]:
    with _component_locks_guard:
        lock = _component_locks.setdefault(component_id, threading.Lock())
    with lock:
        yield


def _ask(
    shop: str, clock: Callable[[], float], call: Callable[[], ProductData]
) -> ProductData:
    """``call()`` against ``shop``, unless that shop was found down a moment ago."""
    with _down_lock:
        until, reason = _down.get(shop, (0.0, ""))
    if clock() < until:
        raise ValidationError(f"{reason} (not asked again for a while)")
    try:
        product = call()
    except ShopLookupMiss:
        raise
    except ValidationError as exc:
        if _UNAVAILABLE.search(str(exc)):
            with _down_lock:
                _down[shop] = (clock() + SHOP_DOWN_SECONDS, str(exc))
        raise
    with _down_lock:
        _down.pop(shop, None)
    return product


def attach_photo(
    session: Session,
    component_id: int,
    image_url: str | None,
    *,
    source_url: str | None = None,
    own_shop: tuple[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Attachment, bool]:
    """Give the component a product photo; ``(attachment, created)``.

    ``image_url`` is what the import's own shop offered, tried first; it may be
    absent, since a shop with no picture of a part may sell it all the same.
    Then each fallback shop is asked by the component's MPN — except the one
    ``source_url`` came from, which would only offer the same picture again —
    and its image used if the answer is this part.

    ``own_shop`` — ``(shop key, that shop's catalogue number for the part)`` —
    is for a caller that knows where the part was bought but not its image, such
    as an invoice line: with no ``image_url``, that shop is asked by that number
    first (the lookup the invoice import makes) and its image tried, then the
    fallbacks.

    Requests for one component take turns (see ``_one_at_a_time``).

    A component that already has a photo gets it back with ``created`` False,
    and nothing is fetched: a retried or doubled request must not stack copies.

    Raises ValidationError, naming what each source said, when nothing could be
    had — and before any shop is asked, for a deleted component.
    """
    component = require_entity(session, Component, component_id, "component")
    refuse_if_deleted(component, component.mpn or f"component #{component_id}")
    with _one_at_a_time(component_id):
        return _attach_photo(session, component, image_url, source_url, own_shop, clock)


def _attach_photo(
    session: Session,
    component: Component,
    image_url: str | None,
    source_url: str | None,
    own_shop: tuple[str, str] | None,
    clock: Callable[[], float],
) -> tuple[Attachment, bool]:
    component_id = cast(int, component.id)
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
    if not image_url and own_shop is not None:
        source, number = own_shop
        image_url, reason = _own_shop_image(session, component, source, number, clock)
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
            url, reason = _fallback_image(session, component, shop, clock)
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
    line's own catalogue number (the key the import looked it up by). A line
    with no such number, or a supplier no provider serves (a hand-made invoice),
    goes straight to the fallbacks, every one of them asked by MPN and checked in
    full. A component that already has a photo is returned as it is, with no
    shop asked, so every line can simply be offered.

    Only for a finalized invoice: a draft's line can still be pointed at another
    component, and its picture would stay on the one it left.
    """
    line = session.get(InvoiceLine, line_id)
    if line is None or line.invoice_id != invoice_id:
        raise NotFoundError(f"invoice line {line_id} not found")
    invoice = require_entity(session, Invoice, invoice_id, "invoice")
    if not invoice.is_finalized:
        raise ValidationError("finalize the invoice first — its lines can still change")
    shop = shops.shop_key_by_name(invoice.supplier)
    number = (line.supplier_part_number or "").strip()
    return attach_photo(
        session,
        line.component_id,
        None,
        own_shop=(shop, number) if shop and number else None,
    )


def _own_shop_image(
    session: Session,
    component: Component,
    shop: str,
    number: str,
    clock: Callable[[], float],
) -> tuple[str | None, str]:
    """The image the part's own shop shows for it, by that shop's catalogue number.

    Checked more leniently than a fallback's answer: the part was bought there
    under that very number, so a maker named on one side only does not refuse
    it, and an answer naming no MPN (TME may not) is taken on the number alone. A
    stated different MPN or a plainly different maker still does. The number is
    the shop's own and nothing else — never the MPN standing in for it, which
    could name another product in the shop's catalogue (TME looks it up as a
    symbol), and then nothing would be left to tell.
    """
    try:
        product = _ask(
            shop,
            clock,
            lambda: shops.import_by_index(
                shop, [number], manufacturer=component.manufacturer
            ),
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
    session: Session, component: Component, shop: str, clock: Callable[[], float]
) -> tuple[str | None, str]:
    """``(image URL, "")`` when ``shop`` sells this very part, else ``(None, why)``."""
    try:
        product = _ask(
            shop,
            clock,
            lambda: shops.import_by_mpn(
                shop, component.mpn or "", manufacturer=component.manufacturer
            ),
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

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
from dataclasses import dataclass, field
from typing import cast

from sqlmodel import Session

from app import config
from app.models.attachment import Attachment
from app.models.component import Component
from app.models.enums import AttachmentKind, LinkKind
from app.models.invoice import Invoice, InvoiceLine
from app.services import attachment_service, link_service, shops, url_fetch
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
_component_locks: dict[int, threading.RLock] = {}
_component_locks_guard = threading.Lock()


@contextmanager
def _one_at_a_time(component_id: int) -> Iterator[None]:
    with _component_locks_guard:
        # Re-entrant: filling an invoice line in holds it, then attaches a photo.
        lock = _component_locks.setdefault(component_id, threading.RLock())
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
    source_shop: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Attachment, bool]:
    """Give the component a product photo; ``(attachment, created)``.

    ``image_url`` is what the import's own shop offered, tried first; it may be
    absent, since a shop with no picture of a part may sell it all the same.
    Then each fallback shop is asked by the component's MPN — except the one
    ``source_url`` came from, which would only offer the same picture again —
    and its image used if the answer is this part. ``source_shop`` names that
    shop by its key instead, for a caller that has no URL for it.

    Requests for one component take turns (see ``_one_at_a_time``).

    A component that already has a photo gets it back with ``created`` False,
    and nothing is fetched: a retried or doubled request must not stack copies.

    Raises ValidationError, naming what each source said, when nothing could be
    had — and before any shop is asked, for a deleted component.
    """
    component = require_entity(session, Component, component_id, "component")
    refuse_if_deleted(component, component.mpn or f"component #{component_id}")
    with _one_at_a_time(component_id):
        return _attach_photo(
            session,
            component,
            image_url,
            source_shop or shops.shop_key(source_url),
            clock,
        )


def _attach_photo(
    session: Session,
    component: Component,
    image_url: str | None,
    source: str | None,
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


@dataclass
class FillIn:
    """What filling an invoice line's component in came to, per thing.

    Each of ``photo``, ``datasheet`` and ``shop_link`` is ``"present"`` (it had
    one, nothing was asked), ``"added"``, ``"linked"`` (a datasheet the shop
    would not let us download, kept as a link) or ``"none"``.
    """

    photo: str = "present"
    datasheet: str = "present"
    shop_link: str = "present"
    reasons: list[str] = field(default_factory=list)


def fill_in_invoice_line(
    session: Session,
    invoice_id: int,
    line_id: int,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> FillIn:
    """Give an invoice line's component what a shop import would have given it.

    A component created at finalize is built from the line's fields alone — the
    import asked the shop, but kept none of the product page, the datasheet or
    the picture it answered with. This asks the invoice's shop again, ONCE, by
    the line's own catalogue number, and adds from that answer whichever of the
    three the component lacks: the product page as a ``shop`` link, the
    datasheet as a file (or as a link, when the shop will not let the server
    download it — the import dialog's rule), and the photo, falling back to
    other shops (:func:`attach_photo`). What the component already has is left
    alone, and one with all three asks no shop at all, so every line of an
    invoice can simply be offered, finalized long ago or just now.

    A line with no catalogue number of its own, or a supplier no provider
    serves, cannot be looked up at its shop: its photo comes from the fallbacks,
    checked in full, and nothing else is filled in. Only for a finalized
    invoice: a draft's line can still be pointed at another component.
    """
    line = session.get(InvoiceLine, line_id)
    if line is None or line.invoice_id != invoice_id:
        raise NotFoundError(f"invoice line {line_id} not found")
    invoice = require_entity(session, Invoice, invoice_id, "invoice")
    if not invoice.is_finalized:
        raise ValidationError("finalize the invoice first — its lines can still change")
    component = require_entity(session, Component, line.component_id, "component")
    refuse_if_deleted(component, component.mpn or f"component #{component.id}")
    with _one_at_a_time(line.component_id):
        return _fill_in(session, component, invoice, line, clock)


def _fill_in(
    session: Session,
    component: Component,
    invoice: Invoice,
    line: InvoiceLine,
    clock: Callable[[], float],
) -> FillIn:
    component_id = cast(int, component.id)
    files = attachment_service.list_attachments(
        session, entity_type="component", entity_id=component_id
    )
    links = link_service.list_links(
        session, entity_type="component", entity_id=component_id
    )
    result = FillIn()
    if not any(a.kind is AttachmentKind.PHOTO for a in files):
        result.photo = "none"
    if not (
        any(a.kind is AttachmentKind.DATASHEET for a in files)
        or any(k.kind is LinkKind.DATASHEET for k in links)
    ):
        result.datasheet = "none"
    if not any(k.kind is LinkKind.SHOP for k in links):
        result.shop_link = "none"
    if "none" not in (result.photo, result.datasheet, result.shop_link):
        return result

    shop = shops.shop_key_by_name(invoice.supplier)
    number = (line.supplier_part_number or "").strip()
    product: ProductData | None = None
    if shop and number:
        product, reason = _own_shop_product(session, component, shop, number, clock)
        if product is None:
            result.reasons.append(f"{shop}: {reason}")

    def link(kind: LinkKind, url: str) -> bool:
        try:
            link_service.create_link(
                session,
                entity_type="component",
                entity_id=component_id,
                kind=kind,
                url=url,
            )
        except ValidationError as exc:
            result.reasons.append(f"{kind.value} link: {exc}")
            return False
        return True

    if result.shop_link == "none":
        # The shop's own product page when it named one; else the page built
        # from the line's number — a search for most shops, but the line's own.
        page = (product.source_url if product else None) or (
            shops.product_url(shop, number) if shop and number else None
        )
        if page and link(LinkKind.SHOP, page):
            result.shop_link = "added"

    if result.datasheet == "none" and product is not None and product.datasheet_url:
        try:
            data, filename = url_fetch.fetch_url(product.datasheet_url)
            attachment_service.create_attachment(
                session,
                entity_type="component",
                entity_id=component_id,
                kind=AttachmentKind.DATASHEET,
                filename=filename,
                data=data,
            )
            result.datasheet = "added"
        except ValidationError as exc:
            # TME's document host and Mouser's Akamai refuse a server; the
            # import dialog keeps such a datasheet as a link, and so does this.
            result.reasons.append(f"datasheet: {exc}")
            if link(LinkKind.DATASHEET, product.datasheet_url):
                result.datasheet = "linked"

    if result.photo == "none":
        try:
            attach_photo(
                session,
                component_id,
                product.image_url if product else None,
                source_shop=shop,
                clock=clock,
            )
            result.photo = "added"
        except ValidationError as exc:
            result.reasons.append(str(exc))
    return result


def _own_shop_product(
    session: Session,
    component: Component,
    shop: str,
    number: str,
    clock: Callable[[], float],
) -> tuple[ProductData | None, str]:
    """The part's own shop's record of it, by that shop's catalogue number.

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
    return product, ""


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

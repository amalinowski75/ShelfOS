"""Fill an invoice line's component in: its shop link, datasheet and photo.

A component created when an invoice is finalized is built from the invoice line
alone. The import asked the shop about the part, but kept none of the product
page, datasheet or picture it answered with — the staged line stores the text
fields, and a new column for the rest would force every installation to rebuild
its database (ShelfOS has no migrations). So this asks the invoice's shop again,
once per line, and adds from that answer whatever the component lacks.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from sqlmodel import Session

from app import config
from app.models.component import Component
from app.models.enums import AttachmentKind, FillStatus, LinkKind
from app.models.invoice import Invoice, InvoiceLine
from app.services import attachment_service, link_service, shops, url_fetch
from app.services import manufacturer_service as ms
from app.services import shop_files_service as shop_files
from app.services._common import normalize, refuse_if_deleted, require_entity
from app.services.errors import NotFoundError, ValidationError
from app.services.shops.base import ProductData

# A datasheet download that failed for a reason worth trying again later: the
# moment, not the shop's policy. Such a datasheet is NOT kept as a link — a link
# counts as having one, so it would never be downloaded after all. Anything else
# (a block page, a 4xx, a file too large to keep) is the shop's answer, and the
# import dialog's rule applies: keep the datasheet as a link.
_RETRY_LATER = re.compile(
    r"timed out|too many downloads|could not reach the server"
    r"|host could not be resolved|\(HTTP (429|5\d\d)\)",
    re.IGNORECASE,
)


@dataclass
class FillIn:
    """What filling an invoice line's component in came to, per thing."""

    photo: FillStatus = "present"
    datasheet: FillStatus = "present"
    shop_link: FillStatus = "present"
    #: Why, for whatever is still missing.
    reasons: list[str] = field(default_factory=list)


def fill_in_invoice_line(
    session: Session,
    invoice_id: int,
    line_id: int,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> FillIn:
    """Give an invoice line's component what a shop import would have given it.

    Asks the invoice's shop ONCE, by the line's own catalogue number, and adds
    from that answer whichever of three things the component lacks: the product
    page as a ``shop`` link, the datasheet and the photo — each from that shop,
    else from Digi-Key or element14 (``shop_files_service``), and a datasheet no
    shop would hand over kept as a link to the invoice shop's own (the import
    dialog's rule). What the component already has
    is left alone, and one with all three asks no shop at all, so every line of
    an invoice can simply be offered, finalized long ago or just now. All of it
    shares one time budget, and the component's turn (``one_at_a_time``).

    A line with no catalogue number of its own, or a supplier no provider
    serves, cannot be looked up at its shop: its datasheet and photo come from
    the fallbacks, checked in full, and it gets no shop link. Only for a finalized
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
    with shop_files.one_at_a_time(
        line.component_id, AttachmentKind.PHOTO, AttachmentKind.DATASHEET
    ):
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

    deadline = clock() + config.ATTACHMENT_URL_TOTAL_TIMEOUT
    shop = shops.shop_key_by_name(invoice.supplier)
    number = (line.supplier_part_number or "").strip()
    # The shop this line can be looked up at, by its own number — or none.
    asked = shop if shop and number else None
    product: ProductData | None = None
    if asked:
        product, reason = _own_shop_product(session, component, asked, number, clock)
        if product is None:
            result.reasons.append(f"{asked}: {reason}")

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

    if result.shop_link == "none" and asked:
        # The shop's own product page when it named one; else the page built
        # from the line's number — a search for most shops, but the line's own.
        page = (product.source_url if product else None) or shops.product_url(
            asked, number
        )
        if page and link(LinkKind.SHOP, page):
            result.shop_link = "added"

    # The fallback shops' answers, shared by the datasheet's search and the
    # photo's, so each shop is asked once for both.
    answers: shop_files.Answers = {}

    if result.datasheet == "none":
        own = product.datasheet_url if product else None
        refusal: ValidationError | None = None
        if own:
            try:
                data, filename = url_fetch.fetch_url(own, deadline=deadline)
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
                refusal = exc
        if result.datasheet == "none":
            # TME's document host and Mouser's Akamai refuse a server; Digi-Key
            # or element14 selling the same part may well hand over the file.
            try:
                shop_files.attach_from_shops_held(
                    session,
                    component,
                    AttachmentKind.DATASHEET,
                    None,
                    source=asked,
                    deadline=deadline,
                    clock=clock,
                    answers=answers,
                    tried=[own] if own else None,
                )
                result.datasheet = "added"
            except ValidationError as exc:
                if refusal is not None:
                    result.reasons.append(f"datasheet: {refusal}")
                result.reasons.append(str(exc))
        # No file from anywhere: keep the shop's own as a link, as the import
        # dialog does — unless its download only failed for the moment (a
        # timeout, a busy server), since a link would count as having one and
        # the file would never be fetched after all.
        if (
            result.datasheet == "none"
            and own
            and refusal is not None
            and not _RETRY_LATER.search(str(refusal))
            and link(LinkKind.DATASHEET, own)
        ):
            result.datasheet = "linked"

    if result.photo == "none":
        if product is not None and not product.image_url:
            result.reasons.append(f"{asked}: no image for this part")
        try:
            # The turn is held and the photo known to be missing: neither again.
            shop_files.attach_from_shops_held(
                session,
                component,
                AttachmentKind.PHOTO,
                product.image_url if product else None,
                # Only the shop actually asked is spared as a fallback; one never
                # asked (no number on the line) may well have the picture.
                source=asked,
                deadline=deadline,
                clock=clock,
                answers=answers,
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
        product = shop_files.ask(
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

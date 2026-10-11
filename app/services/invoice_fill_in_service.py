"""Fill an invoice line's component in: its shop link, datasheet and photo.

A component created when an invoice is finalized is built from the invoice line
alone. The import asked the shop about the part, but kept none of the product
page, datasheet or picture it answered with — the staged line stores the text
fields, and a new column for the rest would force every installation to rebuild
its database (ShelfOS has no migrations). So this asks the invoice's shop again,
once per line, and adds from that answer whatever the component lacks.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from sqlmodel import Session

from app import config
from app.models.component import Component
from app.models.enums import AttachmentKind, FillStatus, LinkKind
from app.models.invoice import Invoice, InvoiceLine
from app.services import attachment_service, link_service, shops
from app.services import manufacturer_service as ms
from app.services import shop_files_service as shop_files
from app.services._common import normalize, refuse_if_deleted, require_entity
from app.services.errors import NotFoundError, ValidationError
from app.services.shops.base import ProductData


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
    # A datasheet FILE is what counts — the rule the import dialog's search uses
    # too. A link alone (a shop that refused the download, before other shops
    # were asked) is not the file, and may now be had from one of them.
    if not any(a.kind is AttachmentKind.DATASHEET for a in files):
        result.datasheet = "none"
    datasheet_linked = any(k.kind is LinkKind.DATASHEET for k in links)
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

    if result.photo == "none" and product is not None and not product.image_url:
        result.reasons.append(f"{asked}: no image for this part")
    # Both files in one search: each fallback shop asked once, and the photo
    # first. Only the shop actually asked is spared as a fallback; one never
    # asked (no number on the line) may well have the file.
    found = shop_files.find_files_held(
        session,
        component,
        photo=result.photo == "none",
        datasheet=result.datasheet == "none",
        image_url=product.image_url if product else None,
        datasheet_url=product.datasheet_url if product else None,
        source=asked,
        deadline=deadline,
        clock=clock,
    )
    result.photo, result.datasheet = found.photo, found.datasheet
    result.reasons.extend(r for r in found.reasons if r not in result.reasons)
    if result.datasheet == "none":
        # No file from anywhere: point at it instead, as the import dialog does.
        # A link never stands in for the file — the next run looks again.
        own = product.datasheet_url if product else None
        if datasheet_linked or (own and link(LinkKind.DATASHEET, own)):
            result.datasheet = "linked"
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

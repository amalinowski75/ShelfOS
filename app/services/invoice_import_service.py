"""Turn a parsed invoice PDF into a draft invoice (core, shop-agnostic).

Orchestrates the per-shop parsers (``app.services.invoice_import``) with the existing
component/invoice services: create a draft invoice, store the PDF, and for each parsed
line either add a real invoice line (component already exists, or its type exists so we
create + enrich the component) or park it as an :class:`InvoiceImportLine` for the user
to resolve on the draft page. Types are never created automatically — that stays a
deliberate, human action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from sqlmodel import Session, col, select

from app.models.enums import AttachmentKind
from app.models.invoice import Invoice, InvoiceImportLine, InvoiceLine
from app.services import attachment_service, invoice_service, shops
from app.services import component_service as cs
from app.services._common import require_entity
from app.services.errors import NotFoundError, ValidationError
from app.services.invoice_import import ParsedInvoice, ParsedLine, parse_invoice
from app.services.shops.base import ProductData, infer_category

_logger = logging.getLogger("shelfos")

_REASON_NO_TYPE = "no matching type — create or pick one"
_REASON_AMBIGUOUS = "several components share this MPN — pick one"
_REASON_NO_MPN = "no part number on the invoice line — match by hand"


@dataclass
class ImportResult:
    """Outcome of an import: the draft invoice and how its lines landed."""

    invoice_id: int
    added: int
    pending: int


def import_invoice(
    session: Session, *, data: bytes, filename: str, user_id: int
) -> ImportResult:
    """Parse a PDF and build a draft invoice, resolving lines where it can.

    Raises ``ValidationError`` on an unreadable/unrecognised PDF or a duplicate
    invoice number (the existing per-supplier unique guard).
    """
    parsed = parse_invoice(data, filename)
    invoice = invoice_service.create_invoice(
        session,
        supplier=parsed.supplier,
        invoice_number=parsed.invoice_number,
        invoice_date=parsed.invoice_date,
        currency=parsed.currency,
        notes=_charges_note(parsed),
    )
    invoice_id = cast(int, invoice.id)

    # Everything after the (committed) invoice is best-effort: the PDF attachment and
    # then a per-line loop that creates components and adds lines, each committing as
    # it goes. If ANY of it raises — a failing shop provider, a concurrently deleted
    # type, a DB error — unwind the whole import so a half-built, un-deletable draft
    # doesn't sit there blocking a re-upload forever (there is no delete-invoice UI).
    try:
        attachment_service.create_attachment(
            session,
            entity_type="invoice",
            entity_id=invoice_id,
            kind=AttachmentKind.INVOICE_PDF,
            filename=filename,
            data=data,
        )
        added = 0
        line_no = 0
        for parsed_line in parsed.lines:
            if parsed_line.kind != "component":
                continue  # a freight/handling charge is noted, not a component line
            line_no += 1
            added_line = _resolve_line(
                session, invoice_id, parsed.shop_key, parsed_line, line_no, user_id
            )
            if added_line:
                added += 1
    except Exception:
        _discard_invoice(session, invoice_id)
        raise

    pending = len(list_pending(session, invoice_id))
    return ImportResult(invoice_id=invoice_id, added=added, pending=pending)


def _discard_invoice(session: Session, invoice_id: int) -> None:
    """Remove a partially-built invoice: its PDF, lines, staged rows and the row.

    Runs after a mid-import failure. Rolls back first so the session isn't stuck in a
    failed transaction, then deletes in a fresh one; a component auto-created earlier
    (already committed) is intentionally left — it is a real part, just not this
    invoice's to own.
    """
    session.rollback()
    attachment_service.delete_attachments_for(
        session, entity_type="invoice", entity_id=invoice_id
    )
    for line in session.exec(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)
    ).all():
        session.delete(line)
    for staged in session.exec(
        select(InvoiceImportLine).where(InvoiceImportLine.invoice_id == invoice_id)
    ).all():
        session.delete(staged)
    invoice = session.get(Invoice, invoice_id)
    if invoice is not None:
        session.delete(invoice)
    session.commit()


def _resolve_line(
    session: Session,
    invoice_id: int,
    shop_key: str,
    line: ParsedLine,
    line_no: int,
    user_id: int,
) -> bool:
    """Add a real line if we can, else stage it. Returns True if a line was added."""

    def add(component_id: int) -> bool:
        invoice_service.add_line(
            session,
            invoice_id,
            component_id=component_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            supplier_part_number=line.supplier_part_number,
        )
        return True

    # 1. When the invoice already names a manufacturer (TME, Digi-Key), a
    #    Manufacturer+MPN match is authoritative — take it without an API call.
    if line.manufacturer and line.mpn:
        hit = cs.find_duplicate_component(
            session, mpn=line.mpn, manufacturer=line.manufacturer
        )
        if hit is not None:
            return add(cast(int, hit.id))

    # 2. Enrich (Mouser/Digi-Key by MPN; TME from the text). This is what fills in a
    #    manufacturer the invoice omitted (Mouser), so matching happens AFTER it —
    #    never guess a same-MPN part by number alone while a real manufacturer is a
    #    lookup away.
    product = _enrich(shop_key, line)
    manufacturer = product.manufacturer or line.manufacturer
    mpn = product.mpn or line.mpn

    # 3. Re-match with the (possibly newly known) manufacturer before creating.
    if manufacturer and mpn:
        hit = cs.find_duplicate_component(
            session, mpn=mpn, manufacturer=manufacturer
        )
        if hit is not None:
            return add(cast(int, hit.id))

    # 4. Still no manufacturer (enrichment unconfigured/failed and the invoice had
    #    none): fall back to a unique MPN-only match. Several same-MPN parts are
    #    genuinely ambiguous and are parked, not guessed.
    if mpn and not manufacturer:
        candidates = cs.find_components_by_mpn(session, mpn)
        if len(candidates) == 1:
            return add(cast(int, candidates[0].id))
        if len(candidates) > 1:
            _stage(session, invoice_id, shop_key, line, line_no, _REASON_AMBIGUOUS)
            return False

    # 5. New part: create it if its type is known, else park it for the user.
    type_id = _resolve_type_id(session, product.category)
    if type_id is not None:
        component = cs.create_component_with_values(
            session,
            type_id,
            manufacturer=manufacturer,
            mpn=mpn,
            package=product.package,
            notes=product.description or line.description,
            user_id=user_id,
        )
        return add(cast(int, component.id))

    reason = _REASON_NO_MPN if not mpn else _REASON_NO_TYPE
    _stage(session, invoice_id, shop_key, line, line_no, reason, product=product)
    return False


def _enrich(shop_key: str, line: ParsedLine) -> ProductData:
    """Best product data for a missing component: the shop API, else the parsed text."""
    provider = shops._BY_MPN.get(shop_key)
    if provider is not None and line.mpn:
        try:
            return provider.fetch_by_mpn(line.mpn)
        except ValidationError as exc:
            # A missing API key or a lookup miss shouldn't sink the whole import —
            # degrade to what the invoice itself told us, and log why.
            _logger.warning("invoice enrich failed for %s: %s", line.mpn, exc)
    return ProductData(
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        description=line.description,
        category=infer_category(line.description, line.manufacturer),
    )


def _resolve_type_id(session: Session, category: str | None) -> int | None:
    """A ShelfOS type id whose name matches ``category`` (case-insensitive), or None."""
    if not category:
        return None
    target = category.strip().casefold()
    for ctype in cs.list_types(session):
        if ctype.name.casefold() == target:
            return cast(int, ctype.id)
    return None


def _stage(
    session: Session,
    invoice_id: int,
    shop_key: str,
    line: ParsedLine,
    line_no: int,
    reason: str,
    *,
    product: ProductData | None = None,
) -> None:
    """Park an unresolved line, prefilled with the best data we have."""
    session.add(
        InvoiceImportLine(
            invoice_id=invoice_id,
            line_no=line_no,
            supplier_part_number=line.supplier_part_number,
            mpn=(product.mpn if product else None) or line.mpn,
            manufacturer=(
                (product.manufacturer if product else None) or line.manufacturer
            ),
            description=(product.description if product else None) or line.description,
            package=product.package if product else None,
            quantity=line.quantity,
            unit_price=line.unit_price,
            shop_key=shop_key,
            reason=reason,
        )
    )
    session.commit()


def _charges_note(parsed: ParsedInvoice) -> str | None:
    """A note listing freight/handling charges that aren't itemised as lines."""
    charges = [line for line in parsed.lines if line.kind != "component"]
    if not charges:
        return None
    parts = [
        f"{(charge.description or 'Shipping').strip()} "
        f"{charge.unit_price} {parsed.currency}"
        for charge in charges
    ]
    return "Imported from PDF. Charges not added as lines: " + "; ".join(parts)


def list_pending(session: Session, invoice_id: int) -> list[InvoiceImportLine]:
    """The outstanding import lines for an invoice, in invoice order."""
    return list(
        session.exec(
            select(InvoiceImportLine)
            .where(InvoiceImportLine.invoice_id == invoice_id)
            .order_by(col(InvoiceImportLine.line_no))
        ).all()
    )


def get_pending(
    session: Session, invoice_id: int, import_line_id: int
) -> InvoiceImportLine:
    """A single pending line, verified to belong to ``invoice_id`` (IDOR guard)."""
    staging = require_entity(
        session, InvoiceImportLine, import_line_id, "import line"
    )
    if staging.invoice_id != invoice_id:
        raise NotFoundError("import line not found")
    return staging


def dismiss_pending(session: Session, invoice_id: int, import_line_id: int) -> None:
    """Drop a pending line the user chose not to add."""
    staging = get_pending(session, invoice_id, import_line_id)
    session.delete(staging)
    session.commit()

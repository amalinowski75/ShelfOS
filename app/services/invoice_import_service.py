"""Turn a parsed invoice PDF into a draft invoice (core, shop-agnostic).

Orchestrates the per-shop parsers (``app.services.invoice_import``) with the existing
component/invoice services: create a draft invoice, store the PDF, and for each parsed
line either add a real invoice line (its component already exists) or **stage** it for
review as an :class:`InvoiceImportLine`. New components are **not** created during
import — a staged row carries the parsed/enriched data plus an auto-inferred ``type_id``
where possible, and the user reviews it on the draft (fixing the type, setting a
location). :func:`materialize_ready_lines` then creates the component + line at
**finalize**, so the catalog is never polluted unless the invoice is actually committed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlmodel import Session, col, select

from app.models.component import ComponentType
from app.models.enums import AttachmentKind
from app.models.invoice import Invoice, InvoiceImportLine, InvoiceLine
from app.models.location import Location
from app.services import attachment_service, invoice_service, shops
from app.services import component_service as cs
from app.services._common import require_entity
from app.services.errors import NotFoundError, ValidationError
from app.services.invoice_import import ParsedInvoice, ParsedLine, parse_invoice
from app.services.shops.base import ProductData, infer_category

_logger = logging.getLogger("shelfos")

# Sentinel for "field not provided" in update_pending (so None can mean "clear it").
_UNSET: Any = object()

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

    # 5. New part: stage it for review — the component is NOT created now, only at
    #    finalize. If its type could be inferred the row is "ready" (needs just a
    #    location); otherwise it needs the user to pick/create a type. Either way the
    #    auto-guess is visible and correctable before anything is committed.
    type_id = _resolve_type_id(session, product.category)
    reason = "" if type_id else (_REASON_NO_MPN if not mpn else _REASON_NO_TYPE)
    _stage(
        session, invoice_id, shop_key, line, line_no, reason,
        product=product, type_id=type_id,
    )
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
    type_id: int | None = None,
) -> None:
    """Park a line for review, prefilled with the best data we have.

    ``type_id`` set marks the row "ready" (auto-classified); left None it needs the
    user to choose a type. Nothing is created here — materialisation is at finalize.
    """
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
            type_id=type_id,
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


def update_pending(
    session: Session,
    invoice_id: int,
    import_line_id: int,
    *,
    type_id: int | None = _UNSET,
    location_id: int | None = _UNSET,
    quantity: int = _UNSET,
    unit_price: Decimal = _UNSET,
) -> InvoiceImportLine:
    """Edit a staged line during review (only the fields provided are changed).

    Setting ``type_id`` marks the row ready (clears its review reason); setting it to
    ``None`` sends it back to needs-review. ``location_id`` is required before the
    invoice can be finalized. Nothing is created here — the component is still made at
    finalize.
    """
    staging = get_pending(session, invoice_id, import_line_id)
    if type_id is not _UNSET:
        if type_id is not None:
            require_entity(session, ComponentType, type_id, "component type")
        staging.type_id = type_id
        staging.reason = "" if type_id is not None else _REASON_NO_TYPE
    if location_id is not _UNSET:
        if location_id is not None:
            require_entity(session, Location, location_id, "location")
        staging.location_id = location_id
    if quantity is not _UNSET:
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        staging.quantity = quantity
    if unit_price is not _UNSET:
        if unit_price < 0:
            raise ValidationError("unit price must not be negative")
        staging.unit_price = unit_price
    session.add(staging)
    session.commit()
    session.refresh(staging)
    return staging


def materialize_ready_lines(
    session: Session, invoice_id: int, user_id: int
) -> None:
    """Turn every ready staged row into a real component + invoice line (at finalize).

    A "ready" row has a ``type_id``; it must also have a ``location_id``. For each, we
    re-match an existing component by Manufacturer+MPN (one may have been created since
    import) and reuse it, else create it, then add the invoice line and delete the
    staging row (via ``add_line``'s ``import_line_id`` reconcile). Rows with no
    ``type_id`` are left for the finalize guard to reject. This deliberately runs
    BEFORE finalize claims the invoice, so it is naturally idempotent: it consumes the
    staging rows, so a retry finds none left to materialise.
    """
    for row in list_pending(session, invoice_id):
        if row.type_id is None:
            continue  # still needs a type — finalize_invoice blocks on these
        if row.location_id is None:
            raise ValidationError(
                f"assign a location to {row.mpn or 'the imported line'} "
                "before finalizing"
            )
        component_id = _materialize_component(session, row, user_id)
        invoice_service.add_line(
            session,
            invoice_id,
            component_id=component_id,
            quantity=row.quantity,
            unit_price=row.unit_price,
            supplier_part_number=row.supplier_part_number,
            location_id=row.location_id,
            import_line_id=cast(int, row.id),
        )


def _materialize_component(
    session: Session, row: InvoiceImportLine, user_id: int
) -> int:
    """Reuse an existing matching component, or create one from the staged row."""
    existing = None
    if row.manufacturer and row.mpn:
        existing = cs.find_duplicate_component(
            session, mpn=row.mpn, manufacturer=row.manufacturer
        )
    if existing is None and row.mpn and not row.manufacturer:
        candidates = cs.find_components_by_mpn(session, row.mpn)
        if len(candidates) == 1:
            existing = candidates[0]
    if existing is not None:
        return cast(int, existing.id)
    component = cs.create_component_with_values(
        session,
        cast(int, row.type_id),
        manufacturer=row.manufacturer,
        mpn=row.mpn,
        package=row.package,
        notes=row.description,
        user_id=user_id,
    )
    return cast(int, component.id)

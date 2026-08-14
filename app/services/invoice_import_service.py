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
from typing import Any, cast

from sqlmodel import Session, col, select

from app.models.component import ComponentType
from app.models.enums import AttachmentKind, MountingType
from app.models.invoice import Invoice, InvoiceImportLine, InvoiceLine
from app.models.location import Location
from app.services import attachment_service, invoice_service, shops
from app.services import component_service as cs
from app.services._common import require_entity
from app.services.errors import (
    InvoiceFinalizedError,
    NotFoundError,
    ValidationError,
)
from app.services.invoice_import import ParsedInvoice, ParsedLine, parse_invoice
from app.services.match_rule_service import RuleSet, load_rules
from app.services.matching import MatchProposal, build_proposal
from app.services.shops.base import ProductData, ShopLookupMiss, infer_category

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
        # Shops whose enrichment hard-failed during THIS import (unconfigured,
        # unreachable, rejecting): every later line would fail identically, so one
        # warning is burned and the rest of the import skips the API instead of
        # stacking a full timeout per line. A plain per-part miss does NOT trip this.
        enrich_disabled: set[str] = set()
        # The matching rules are the same for every line — load them once.
        rules = load_rules(session)
        for parsed_line in parsed.lines:
            if parsed_line.kind != "component":
                continue  # a freight/handling charge is noted, not a component line
            line_no += 1
            added_line = _resolve_line(
                session,
                invoice_id,
                parsed.shop_key,
                parsed_line,
                line_no,
                user_id,
                enrich_disabled,
                rules,
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
    enrich_disabled: set[str],
    rules: RuleSet,
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
    product = _enrich(shop_key, line, enrich_disabled)
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
    #    finalize. The matching engine works out the type, mounting and as many
    #    parameter values as it can from the enriched data + the invoice text; if it
    #    found a type the row is "ready" (needs just a location), otherwise the user
    #    picks one. Every guess is visible and correctable before anything is committed.
    proposal = build_proposal(session, product, rules=rules)
    reason = (
        "" if proposal.type_id else (_REASON_NO_MPN if not mpn else _REASON_NO_TYPE)
    )
    _stage(
        session, invoice_id, shop_key, line, line_no, reason,
        product=product, proposal=proposal,
    )
    return False


def _enrich(
    shop_key: str, line: ParsedLine, enrich_disabled: set[str]
) -> ProductData:
    """Best product data for a missing component: the shop API, else the parsed text.

    Keyed by the shop's OWN catalogue index first (Mouser SKU, Digi-Key "…-ND"
    number, TME symbol) — it sits in the strict item row itself, so it survives a
    mangled description cell that would corrupt or lose the parsed MPN — with the
    MPN as the second candidate. The canonical MPN/manufacturer then come from the
    API's answer; the invoice text remains the fallback when no lookup succeeds.

    A per-part miss degrades just this line. Any other failure (unconfigured,
    unreachable, rejected) marks the shop in ``enrich_disabled`` so the REST of the
    import skips the API — it would fail identically per line, each burning a full
    round-trip or timeout inside one synchronous request.
    """
    provider = shops._BY_INDEX.get(shop_key)
    candidates = [
        key
        for key in dict.fromkeys((line.supplier_part_number, line.mpn))
        if key
    ]
    if provider is not None and candidates and shop_key not in enrich_disabled:
        try:
            return provider.fetch_by_index(candidates)
        except ShopLookupMiss as exc:
            _logger.info("no shop product for %s: %s", candidates, exc)
        except ValidationError as exc:
            enrich_disabled.add(shop_key)
            _logger.warning(
                "shop enrichment disabled for the rest of this import (%s): %s",
                shop_key,
                exc,
            )
    return ProductData(
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        description=line.description,
        category=infer_category(line.description, line.manufacturer),
    )


def _stage(
    session: Session,
    invoice_id: int,
    shop_key: str,
    line: ParsedLine,
    line_no: int,
    reason: str,
    *,
    product: ProductData | None = None,
    proposal: MatchProposal | None = None,
) -> None:
    """Park a line for review, prefilled with the best data we have.

    A ``proposal`` from the matching engine carries the inferred type, mounting,
    package and parameter values; without one (an ambiguous MPN match) the row is
    parked bare. Nothing is created here — materialisation is at finalize.
    """
    parameters = (
        [{"parameter_definition_id": pid, "value": value}
         for pid, value in proposal.parameters]
        if proposal
        else []
    )
    package = (proposal.package if proposal else None) or (
        product.package if product else None
    )
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
            package=package,
            quantity=line.quantity,
            unit_price=line.unit_price,
            shop_key=shop_key,
            type_id=proposal.type_id if proposal else None,
            mounting_type=(
                proposal.mounting_type if proposal and proposal.mounting_type
                else MountingType.OTHER
            ),
            parameters=parameters,
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


def _require_draft_invoice(session: Session, invoice_id: int) -> None:
    """Refuse to mutate a staged row once its invoice is finalized (read-only).

    Mirrors ``invoice_service.set_line_location``'s guard: a finalized invoice's rows
    must not change, and this also closes the review-PATCH-during-finalize window from
    the writer's side (the materialise loop re-validates too, belt-and-suspenders).
    """
    invoice = invoice_service.get_invoice(session, invoice_id)
    if invoice.is_finalized:
        raise InvoiceFinalizedError(f"invoice {invoice_id} is finalized (read-only)")


def dismiss_pending(session: Session, invoice_id: int, import_line_id: int) -> None:
    """Drop a pending line the user chose not to add."""
    _require_draft_invoice(session, invoice_id)
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
    manufacturer: str | None = _UNSET,
    mpn: str | None = _UNSET,
    package: str | None = _UNSET,
    mounting_type: MountingType = _UNSET,
    description: str | None = _UNSET,
    parameters: list[dict[str, Any]] | None = _UNSET,
) -> InvoiceImportLine:
    """Edit a staged line during review (only the fields provided are changed).

    Setting ``type_id`` marks the row ready (clears its review reason); setting it to
    ``None`` sends it back to needs-review. Changing the type also clears ``parameters``
    (they are the old type's). ``location_id`` is required before finalize. The identity
    fields + parameters seed the component created at finalize. Nothing is created here.
    """
    _require_draft_invoice(session, invoice_id)
    staging = get_pending(session, invoice_id, import_line_id)
    if type_id is not _UNSET:
        if type_id is not None:
            require_entity(session, ComponentType, type_id, "component type")
        if type_id != staging.type_id:
            staging.parameters = []  # entered for the old type — no longer valid
        staging.type_id = type_id
        staging.reason = "" if type_id is not None else _REASON_NO_TYPE
    if location_id is not _UNSET:
        if location_id is not None:
            require_entity(session, Location, location_id, "location")
        staging.location_id = location_id
    if manufacturer is not _UNSET:
        staging.manufacturer = manufacturer
    if mpn is not _UNSET:
        staging.mpn = mpn
    if package is not _UNSET:
        staging.package = package
    if mounting_type is not _UNSET:
        staging.mounting_type = mounting_type
    if description is not _UNSET:
        staging.description = description
    if parameters is not _UNSET:
        staging.parameters = parameters or []
    session.add(staging)
    session.commit()
    session.refresh(staging)
    return staging


def materialize_ready_lines(
    session: Session, invoice_id: int, user_id: int
) -> None:
    """Turn every ready staged row into a real component + invoice line (at finalize).

    A "ready" row has a ``type_id`` and a ``location_id`` (both validated by
    ``finalize_invoice`` before this runs). For each, we re-match an existing component
    by Manufacturer+MPN (one may have been created since import) and reuse it, else
    create it, then add the invoice line and delete the staging row.

    **Does not commit** — everything joins finalize's single transaction, so materialise
    plus the atomic ``is_finalized`` claim and the stock movements succeed or roll back
    together. That keeps a concurrent/duplicate finalize from leaving a stray component
    behind: the loser's claim fails and its whole unit (including any component created
    here) rolls back. Rows with no ``type_id`` are left for the finalize guard.
    """
    for row in list_pending(session, invoice_id):
        # Re-validate at the point of use, not against finalize's earlier snapshot: a
        # concurrent review PATCH could have cleared the type or location in between.
        # Raise (never silently skip a row, never build a location-less line) so
        # finalize's transaction rolls back cleanly rather than orphaning the row or
        # 500-ing on a downstream assert.
        if row.type_id is None or row.location_id is None:
            raise ValidationError(
                "an imported line changed during finalize (its type or location was "
                "cleared) — reopen the invoice and finalize again"
            )
        component_id = _materialize_component(session, row, user_id)
        session.add(
            InvoiceLine(
                invoice_id=invoice_id,
                component_id=component_id,
                supplier_part_number=row.supplier_part_number,
                quantity=row.quantity,
                unit_price=row.unit_price,
                total_price=row.unit_price * row.quantity,
                location_id=row.location_id,
            )
        )
        session.delete(row)
    session.flush()  # surface any error now, inside the caller's transaction


def _materialize_component(
    session: Session, row: InvoiceImportLine, user_id: int
) -> int:
    """Reuse an existing matching component, or create one (uncommitted) from a row."""
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
    # The parameter values the user entered during review (validated here against the
    # type's definitions, same as a direct create); an existing reuse ignores them.
    values = [
        (int(p["parameter_definition_id"]), p["value"]) for p in (row.parameters or [])
    ]
    component = cs.create_component_with_values(
        session,
        cast(int, row.type_id),
        manufacturer=row.manufacturer,
        mpn=row.mpn,
        package=row.package,
        mounting_type=row.mounting_type,
        notes=row.description,
        values=values,
        user_id=user_id,
        commit=False,
    )
    return cast(int, component.id)

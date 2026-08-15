"""Component endpoints (spec §4, §12)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    ComponentCreate,
    ComponentScanRead,
    ComponentUpdate,
    ParameterValueSet,
    ScannedComponentRead,
    ScannedStockRead,
    ShopLookup,
)
from app.auth.deps import current_user_id, require_admin
from app.models.component import Component, ComponentParameter
from app.models.user import User
from app.services import component_service as cs
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import DuplicateComponentError
from app.services.shops.scan import parse_scan

router = APIRouter(prefix="/api/components", tags=["components"])


def _scan_identifiers(code: str) -> list[str]:
    """Part numbers a scanned label offers, best first, de-duplicated.

    The manufacturer's own number leads because that is what a component
    stores; a shop's symbol (TME's ``PN:``, a DataMatrix 30P) only matches when
    the two happen to agree, and the URL's segments are the last resort of a
    QR that states nothing outright.
    """
    scan = parse_scan(code)  # ValidationError → 422
    candidates = [scan.manufacturer_pn, scan.mpn, scan.distributor_pn]
    if not any(candidates):
        from app.api.routes.shops import _url_symbols

        candidates = list(_url_symbols(scan.url))
    seen: dict[str, str] = {}
    for value in candidates:
        if value and value.casefold() not in seen:
            seen[value.casefold()] = value
    return list(seen.values())


@router.post("/scan", response_model=ComponentScanRead)
def scan_component(
    payload: ShopLookup,
    session: Session = Depends(get_session),
) -> ComponentScanRead:
    """Resolve a scanned bag label to the component(s) it identifies.

    Used by scan putaway on the components page: one round trip gives the
    match and the stock a relocation would move, so the client never has to
    guess between identifiers or fetch the slots separately.
    """
    identifiers = _scan_identifiers(payload.code)
    matches: list[ScannedComponentRead] = []
    seen_ids: set[int] = set()
    for identifier in identifiers:
        for component in cs.find_components_by_mpn(session, identifier):
            component_id = component.id
            if component_id is None or component_id in seen_ids:
                continue
            seen_ids.add(component_id)
            matches.append(
                ScannedComponentRead(
                    id=component_id,
                    mpn=component.mpn,
                    manufacturer=component.manufacturer,
                    description=component.notes,
                    locations=[
                        ScannedStockRead(
                            id=slot.location_id,
                            path=ls.format_path(session, slot.location_id),
                            quantity=slot.quantity,
                        )
                        for slot in ss.list_component_locations(session, component_id)
                    ],
                )
            )
        if matches:
            break  # a better identifier already answered; don't widen the net
    return ComponentScanRead(identifiers=identifiers, matches=matches)


@router.post("", response_model=Component, status_code=status.HTTP_201_CREATED)
def create_component(
    payload: ComponentCreate,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> Component:
    # Refuse a re-add of a part already in inventory (same MPN + manufacturer). The
    # lookup lives in the service so it stays testable and reusable; enforcement is
    # here rather than in the service so demo-data seeding and direct-service tests,
    # which legitimately create bare/duplicate rows, aren't blocked. This is a
    # best-effort app-level check (like the type/parameter-name uniqueness checks) —
    # there's no DB unique constraint, so two truly-concurrent creates could race;
    # acceptable for this app's single-writer usage.
    existing = cs.find_duplicate_component(
        session, mpn=payload.mpn, manufacturer=payload.manufacturer
    )
    if existing is not None:
        mpn = (payload.mpn or "").strip()
        manufacturer = (payload.manufacturer or "").strip()
        origin = f" from {manufacturer}" if manufacturer else ""
        raise DuplicateComponentError(
            f"A component with MPN {mpn}{origin} already exists.",
            existing_id=existing.id,  # type: ignore[arg-type]
        )
    return cs.create_component_with_values(
        session,
        payload.type_id,
        manufacturer=payload.manufacturer,
        mpn=payload.mpn,
        package=payload.package,
        mounting_type=payload.mounting_type,
        notes=payload.notes,
        values=[(p.parameter_definition_id, p.value) for p in payload.parameters],
        user_id=user_id,
    )


@router.patch("/{component_id}", response_model=Component)
def update_component(
    component_id: int,
    payload: ComponentUpdate,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
) -> Component:
    """Edit a component's mutable fields + parameter values (§12). Admin only.

    The router-level ``require_access``/``require_csrf`` already apply; adding
    ``require_admin`` here restricts editing to admins while create stays open to
    any writer. Type and MPN are immutable (not in ``ComponentUpdate``).
    """
    return cs.update_component(
        session,
        component_id,
        manufacturer=payload.manufacturer,
        package=payload.package,
        mounting_type=payload.mounting_type,
        notes=payload.notes,
        values=[(p.parameter_definition_id, p.value) for p in payload.parameters],
        user_id=admin.id,
    )


@router.get("/{component_id}/parameters", response_model=list[ComponentParameter])
def list_parameter_values(
    component_id: int, session: Session = Depends(get_session)
) -> list[ComponentParameter]:
    return cs.list_parameter_values(session, component_id)


@router.put("/{component_id}/parameters", response_model=ComponentParameter)
def set_parameter_value(
    component_id: int,
    payload: ParameterValueSet,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
) -> ComponentParameter:
    """Set one parameter value. Admin only — editing a component (its fields or its
    values) is an admin action (§12), so this single-value path is gated the same as
    the ``PATCH`` above rather than left at writer level."""
    return cs.set_parameter_value(
        session,
        component_id,
        payload.parameter_definition_id,
        payload.value,
        user_id=admin.id,
    )

"""Manufacturer-name endpoints: "might I already have this part?", and the answer.

Two halves of one interaction. The client asks whether an incoming (MPN, maker)
already exists under a different spelling; if the user says yes to one of the
candidates, the client records the alias so the question is never asked again for
that spelling.

Mounted with the other protected routers, so both need a writer + CSRF. Recording an
alias is not gated on admin even though it changes matching for everyone: it is a
by-product of importing a part, which any writer does, and gating it would leave a
non-admin looking at a warning they cannot resolve.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    ManufacturerAliasCreate,
    ManufacturerAliasRead,
    ManufacturerConflictRead,
    ManufacturerConflictsRead,
)
from app.services import component_service as cs
from app.services import manufacturer_service as ms

router = APIRouter(prefix="/api/manufacturers", tags=["manufacturers"])


@router.get("/conflicts", response_model=ManufacturerConflictsRead)
def manufacturer_conflicts(
    mpn: str,
    manufacturer: str | None = None,
    session: Session = Depends(get_session),
) -> ManufacturerConflictsRead:
    """Parts already in stock with this MPN but a different maker's name.

    A GET because it decides nothing and spends nothing — the dialog calls it as
    the MPN field settles.
    """
    conflicts = cs.find_manufacturer_conflicts(
        session, mpn=mpn, manufacturer=manufacturer
    )
    types = {t.id: t.name for t in cs.list_types(session)} if conflicts else {}
    return ManufacturerConflictsRead(
        manufacturer=ms.canonical_name(session, manufacturer),
        candidates=[
            ManufacturerConflictRead(
                id=component.id,
                mpn=component.mpn,
                manufacturer=component.manufacturer,
                description=component.notes,
                type_name=types.get(component.type_id),
            )
            for component in conflicts
        ],
    )


@router.post("/aliases", response_model=ManufacturerAliasRead | None)
def create_manufacturer_alias(
    payload: ManufacturerAliasCreate,
    session: Session = Depends(get_session),
) -> ManufacturerAliasRead | None:
    """Remember that one spelling means another.

    Answers ``null`` — not an error — when there was nothing worth recording: the
    incoming name was blank, or it already folds to the canonical one. The client
    treats "recorded" and "nothing to record" the same way, so saying so plainly
    beats inventing a failure.
    """
    row = ms.record_alias(
        session, alias=payload.alias, canonical=payload.canonical
    )
    if row is None:
        return None
    return ManufacturerAliasRead(
        id=row.id,
        alias=row.alias,
        canonical=row.canonical,
    )

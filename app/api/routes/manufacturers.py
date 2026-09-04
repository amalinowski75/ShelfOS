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

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    ManufacturerAliasCreate,
    ManufacturerAliasRead,
    SameMpnCandidateRead,
    SameMpnRead,
)
from app.auth.deps import require_admin
from app.models.user import User
from app.services import component_service as cs
from app.services import manufacturer_service as ms

router = APIRouter(prefix="/api/manufacturers", tags=["manufacturers"])


@router.get("/same-mpn", response_model=SameMpnRead)
def parts_sharing_mpn(
    mpn: str,
    manufacturer: str | None = None,
    session: Session = Depends(get_session),
) -> SameMpnRead:
    """Parts already in stock carrying this part number, whoever makes them.

    A GET because it decides nothing and spends nothing — the dialog calls it as
    the MPN field settles. ``manufacturer`` is not a filter: it is echoed back
    resolved through the alias table, so the caller can show what the name it was
    given actually means here.
    """
    found = cs.find_parts_sharing_mpn(session, mpn)
    types = {t.id: t.name for t in cs.list_types(session)} if found else {}
    return SameMpnRead(
        manufacturer=ms.canonical_name(session, manufacturer),
        candidates=[
            SameMpnCandidateRead(
                id=component.id,
                mpn=component.mpn,
                manufacturer=component.manufacturer,
                description=component.notes,
                type_name=types.get(component.type_id),
            )
            for component in found
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


@router.delete("/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_manufacturer_alias(
    alias_id: int,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
) -> None:
    """Forget one spelling.

    Admin-only, though any writer can CREATE one by answering "this is it" during an
    import — the same split the app already makes for component types, which a writer
    adds inline from the dialog and an admin manages on their own page. Creating
    vocabulary is part of doing the work; curating it is not.

    Only future lookups change. Components already stored under the canonical name
    keep it: the alias was never a live indirection, just the rule that decided what
    to write.
    """
    ms.delete_alias(session, alias_id)

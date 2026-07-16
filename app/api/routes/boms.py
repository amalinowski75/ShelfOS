"""KiCad BOM import + availability report (spec §21/§22).

Upload a BOM CSV (writers), list/inspect saved BOMs, and get a live availability
report against current stock. The original CSV is kept as a ``bom`` attachment.
Mounted under the protected routers (read-only accounts can read, not upload/
delete).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlmodel import Session

from app import config
from app.api.deps import get_session
from app.api.schemas import BomDetailRead, BomLineRead, BomRead
from app.auth.deps import current_user_id
from app.models.bom import Bom
from app.services import bom_service as svc
from app.services.errors import ValidationError

router = APIRouter(prefix="/api/boms", tags=["boms"])


@router.post("", response_model=BomRead, status_code=status.HTTP_201_CREATED)
def upload_bom(
    name: str | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> Bom:
    """Parse and store an uploaded KiCad BOM CSV (writers). 201 on success."""
    if file.size is not None and file.size > config.MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            f"file exceeds the {config.MAX_ATTACHMENT_MB} MB limit"
        )
    try:
        data = file.file.read()
    finally:
        file.file.close()
    filename = file.filename or "bom.csv"
    return svc.create_bom(
        session,
        name=(name or "").strip() or filename,
        filename=filename,
        data=data,
        user_id=user_id,
    )


@router.get("", response_model=list[BomRead])
def list_boms(session: Session = Depends(get_session)) -> list[Bom]:
    """List saved BOMs, newest first."""
    return svc.list_boms(session)


@router.get("/{bom_id}", response_model=BomDetailRead)
def get_bom(
    bom_id: int, session: Session = Depends(get_session)
) -> BomDetailRead:
    """A BOM with its parsed lines (metadata only)."""
    bom = svc.get_bom(session, bom_id)
    return BomDetailRead(
        id=bom.id,
        name=bom.name,
        source_filename=bom.source_filename,
        created_at=bom.created_at,
        lines=[
            BomLineRead.model_validate(line)
            for line in svc.get_bom_lines(session, bom_id)
        ],
    )


@router.get("/{bom_id}/report")
def bom_report(
    bom_id: int, session: Session = Depends(get_session)
) -> dict[str, object]:
    """Live availability report: stock status + substitute suggestions (§21)."""
    return svc.build_bom_report(session, bom_id)


@router.delete("/{bom_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bom(bom_id: int, session: Session = Depends(get_session)) -> None:
    """Delete a BOM, its lines and its stored CSV (writers)."""
    svc.delete_bom(session, bom_id)

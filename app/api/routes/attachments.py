"""Attachment upload/download endpoints (spec §10).

Generic over the target entity (``entity_type`` + ``entity_id``): a component or
an invoice can carry attachments. Files are stored on disk by the service; these
routes stay thin. Mounted under the protected routers, so read-only accounts may
list/download (GET) but not upload/delete.

Access is by ``attachment_id`` alone, with no per-attachment ownership check —
consistent with ShelfOS's flat trust model (every authenticated user can see
every entity). If attachments ever hold more sensitive content, revisit this.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from app import config
from app.api.deps import get_session
from app.api.schemas import AttachmentRead
from app.models.attachment import Attachment
from app.models.enums import AttachmentKind
from app.services import attachment_service as svc
from app.services.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


@router.post("", response_model=AttachmentRead, status_code=status.HTTP_201_CREATED)
def upload_attachment(
    entity_type: str = Form(...),
    entity_id: int = Form(...),
    kind: AttachmentKind = Form(AttachmentKind.OTHER),
    notes: str | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> Attachment:
    """Attach an uploaded file to an entity (§10). Writers only."""
    # Reject an oversize upload from its declared size before reading it into
    # memory; the service re-checks the actual bytes (and covers an absent size).
    if file.size is not None and file.size > config.MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            f"attachment exceeds the {config.MAX_ATTACHMENT_MB} MB limit"
        )
    try:
        data = file.file.read()
    finally:
        file.file.close()
    return svc.create_attachment(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        filename=file.filename or "upload",
        data=data,
        notes=notes,
    )


@router.get("", response_model=list[AttachmentRead])
def list_attachments(
    entity_type: str,
    entity_id: int,
    session: Session = Depends(get_session),
) -> list[Attachment]:
    """List an entity's attachments (metadata only)."""
    return svc.list_attachments(session, entity_type=entity_type, entity_id=entity_id)


@router.get("/{attachment_id}/download")
def download_attachment(
    attachment_id: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve the stored file with its original name and a guessed content type."""
    attachment = svc.get_attachment(session, attachment_id)
    path = svc.stored_file_path(attachment)
    if not path.is_file():
        raise NotFoundError("attachment file is not available")
    media_type = (
        mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type, filename=attachment.filename)


@router.get("/{attachment_id}/thumbnail")
def thumbnail_attachment(
    attachment_id: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve a small cached thumbnail (image attachments; else the original)."""
    path = svc.thumbnail_file(session, attachment_id)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachment(
    attachment_id: int,
    session: Session = Depends(get_session),
) -> None:
    """Delete an attachment and its file (§10). Writers only."""
    svc.delete_attachment(session, attachment_id)

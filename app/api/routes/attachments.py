"""Attachment upload/download endpoints (spec §10).

Generic over the target entity (``entity_type`` + ``entity_id``): a component or
an invoice can carry attachments. Files are stored on disk by the service; these
routes stay thin. Mounted under the protected routers, so read-only accounts may
list/download (GET) but not upload/delete.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import AttachmentRead
from app.models.attachment import Attachment
from app.models.enums import AttachmentKind
from app.services import attachment_service as svc
from app.services.errors import NotFoundError

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


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachment(
    attachment_id: int,
    session: Session = Depends(get_session),
) -> None:
    """Delete an attachment and its file (§10). Writers only."""
    svc.delete_attachment(session, attachment_id)

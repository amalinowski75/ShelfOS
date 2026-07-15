"""File-attachment service (spec §10).

Attachments are stored on disk under ``config.ATTACHMENTS_DIR``; the database
(``Attachment``) keeps only metadata and the stored path. The table is generic
(``entity_type`` + ``entity_id``), so a component or an invoice — and any future
entity added to ``_ENTITY_MODELS`` — can carry attachments.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session, SQLModel, col, select

from app import config
from app.models.attachment import Attachment
from app.models.component import Component
from app.models.enums import AttachmentKind
from app.models.invoice import Invoice
from app.services._common import require_entity
from app.services.errors import NotFoundError, ValidationError

# Which entity a ``(entity_type, entity_id)`` pair points at. Anything else is
# rejected, so an attachment can never dangle off a type we don't recognise.
_ENTITY_MODELS: dict[str, type[SQLModel]] = {
    "component": Component,
    "invoice": Invoice,
}

# A short, alphanumeric file extension, e.g. ".pdf" / ".jpg".
_SAFE_EXTENSION = re.compile(r"\.[a-z0-9]{1,10}")

# Bound the free-text metadata so a writer can't bloat the row with a huge
# filename or notes field (the file bytes have their own size cap).
_MAX_FILENAME_LEN = 255
_MAX_NOTES_LEN = 2000


def _entity_model(entity_type: str) -> type[SQLModel]:
    """Return the model for a known ``entity_type`` or raise ``ValidationError``."""
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise ValidationError(f"unknown entity_type {entity_type!r}")
    return model


def _safe_extension(filename: str) -> str:
    """Return a safe, lower-cased extension for the stored name, or ``""``.

    Only the suffix is ever reused, and only when it is short and alphanumeric;
    the stored name itself is UUID-based, so the user's filename can never inject
    a path separator or ``..`` into the on-disk path.
    """
    ext = Path(filename or "").suffix.lower()
    return ext if _SAFE_EXTENSION.fullmatch(ext) else ""


def create_attachment(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    kind: AttachmentKind,
    filename: str,
    data: bytes,
    notes: str | None = None,
) -> Attachment:
    """Store an uploaded file and record its metadata (spec §10).

    ``entity_type`` must be a known entity that actually exists; an empty or
    oversized file is rejected. The bytes are written under a generated name and
    the original ``filename`` is kept for download. The file is written before
    the row is committed, and removed again if the commit fails, so a failed
    insert never leaves an orphan file behind.
    """
    model = _entity_model(entity_type)
    require_entity(session, model, entity_id, entity_type)

    if not data:
        raise ValidationError("attachment file must not be empty")
    if len(data) > config.MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            f"attachment exceeds the {config.MAX_ATTACHMENT_MB} MB limit"
        )
    if len(filename) > _MAX_FILENAME_LEN:
        raise ValidationError(
            f"filename must be at most {_MAX_FILENAME_LEN} characters"
        )
    if notes is not None and len(notes) > _MAX_NOTES_LEN:
        raise ValidationError(f"notes must be at most {_MAX_NOTES_LEN} characters")

    base = config.ATTACHMENTS_DIR
    base.mkdir(parents=True, exist_ok=True)
    stored_name = uuid4().hex + _safe_extension(filename)
    stored_path = base / stored_name
    stored_path.write_bytes(data)

    attachment = Attachment(
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        file_path=stored_name,
        filename=filename,
        notes=notes,
    )
    session.add(attachment)
    try:
        session.commit()
    except Exception:
        session.rollback()
        stored_path.unlink(missing_ok=True)
        raise
    session.refresh(attachment)
    return attachment


def list_attachments(
    session: Session, *, entity_type: str, entity_id: int
) -> list[Attachment]:
    """Return the attachments for one entity, oldest first (metadata only).

    The target must exist (unknown type → 422, unknown id → 404), matching
    ``create_attachment`` rather than silently returning an empty list.
    """
    model = _entity_model(entity_type)
    require_entity(session, model, entity_id, entity_type)
    statement = (
        select(Attachment)
        .where(Attachment.entity_type == entity_type)
        .where(Attachment.entity_id == entity_id)
        .order_by(col(Attachment.id))
    )
    return list(session.exec(statement).all())


def get_attachment(session: Session, attachment_id: int) -> Attachment:
    """Return an attachment row or raise :class:`NotFoundError`."""
    return require_entity(session, Attachment, attachment_id, "attachment")


def stored_file_path(attachment: Attachment) -> Path:
    """Resolve the on-disk path for an attachment, guarding against traversal.

    ``file_path`` is a server-generated name, but resolve it and confirm it stays
    within the store as defence in depth before the bytes are served.
    """
    base = config.ATTACHMENTS_DIR.resolve()
    candidate = (base / attachment.file_path).resolve()
    if not candidate.is_relative_to(base):
        raise NotFoundError("attachment file is not available")
    return candidate


def delete_attachment(session: Session, attachment_id: int) -> None:
    """Delete an attachment row and its file (hard delete).

    Unlink the file first (best-effort), then remove the row. A crash between the
    two then leaves at worst a row with a missing file — which downloads treat as
    404 and a repeat delete cleans up — never a file with no row pointing at it.
    """
    attachment = get_attachment(session, attachment_id)
    path = stored_file_path(attachment)
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    session.delete(attachment)
    session.commit()

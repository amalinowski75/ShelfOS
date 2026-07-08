"""Attachment metadata model (spec §10).

Files live on disk; the database stores only metadata and paths.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from app.models.enums import AttachmentKind, enum_column


class Attachment(SQLModel, table=True):
    __tablename__ = "attachments"

    id: int | None = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    kind: AttachmentKind = Field(sa_column=enum_column(AttachmentKind))
    file_path: str
    filename: str
    notes: str | None = Field(default=None)

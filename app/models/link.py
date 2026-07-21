"""External-link model (categorized clickable URLs for an entity).

A link is deliberately NOT an attachment: it is a URL the user's browser opens, with
no downloaded bytes, no on-disk file, and no thumbnail. It exists so a component can
keep the shop/product page it came from and a datasheet that the vendor blocks from
server-side download (see the TME Cloudflare challenge). The generic
``entity_type``/``entity_id`` pair mirrors :class:`~app.models.attachment.Attachment`
so the same entity dispatch is reused. That dispatch (and the delete-cascade wiring)
accepts ``component``/``invoice``/``bom`` deliberately, even though only the component
page currently mounts a links panel — the backend is wired wider than the UI on
purpose, so an invoice/BOM panel is later a template change alone.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from app.models.enums import LinkKind, enum_column


class Link(SQLModel, table=True):
    __tablename__ = "links"

    id: int | None = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int
    kind: LinkKind = Field(sa_column=enum_column(LinkKind))
    url: str
    label: str | None = Field(default=None)  # display text; falls back to the URL host
    notes: str | None = Field(default=None)

"""External-link endpoints (categorized clickable URLs for an entity).

Generic over the target entity (``entity_type`` + ``entity_id``), like attachments,
but far thinner: a link has no file, so there is no upload, download, or thumbnail —
only create, list, and delete. Mounted under the protected routers, so read-only
accounts may list (GET) but not create/delete.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import LinkCreate, LinkRead
from app.models.link import Link
from app.services import link_service as svc

router = APIRouter(prefix="/api/links", tags=["links"])


@router.post("", response_model=LinkRead, status_code=status.HTTP_201_CREATED)
def create_link(
    payload: LinkCreate, session: Session = Depends(get_session)
) -> Link:
    """Attach an external link. A non-http(s) URL is rejected with 422."""
    return svc.create_link(
        session,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        kind=payload.kind,
        url=payload.url,
        label=payload.label,
        notes=payload.notes,
    )


@router.get("", response_model=list[LinkRead])
def list_links(
    entity_type: str,
    entity_id: int,
    session: Session = Depends(get_session),
) -> list[Link]:
    """List an entity's links, oldest first."""
    return svc.list_links(session, entity_type=entity_type, entity_id=entity_id)


@router.delete("/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(link_id: int, session: Session = Depends(get_session)) -> None:
    """Delete a link. Writers only."""
    svc.delete_link(session, link_id)

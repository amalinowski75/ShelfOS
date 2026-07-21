"""External-link service (categorized clickable URLs for an entity).

Deliberately much simpler than the attachment service: a link is just a URL, so there
are no bytes, no disk, no thumbnails, and no download route. The one genuinely new
rule is URL validation — a stored link becomes a clickable ``href``, so only http/https
are accepted, and ``javascript:``/``data:``/``file:`` are rejected at creation. The URL
is never fetched server-side, so no SSRF guard is needed or wanted here.

The generic ``entity_type``/``entity_id`` dispatch is shared with the attachment
service (``entity_model`` in ``_common``), so links attach to the same entities.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from sqlmodel import Session, col, select

from app.models.enums import LinkKind
from app.models.link import Link
from app.services._common import entity_model, require_entity
from app.services.errors import ValidationError

_MAX_URL_LEN = 2048
_MAX_LABEL_LEN = 255
_MAX_NOTES_LEN = 2000

# Any C0/C1 control character or DEL. urlsplit silently strips leading control
# chars, so without this a value like "\x01http://x" would pass server validation
# yet the browser-side scheme check (safeHref) would reject it — a link that stores
# "valid" but renders as dead text. Rejecting them keeps the two layers aligned.
_CONTROL_CHARS = frozenset(chr(c) for c in [*range(0x00, 0x20), 0x7F])


def _validate_url(url: str) -> str:
    """Return a cleaned http/https URL or raise ``ValidationError``.

    The stored value is later rendered as a clickable ``href``, so anything that
    isn't a plain web URL — ``javascript:``, ``data:``, ``file:``, a scheme-less or
    host-less string — is refused here, at the boundary.
    """
    cleaned = url.strip()
    if not cleaned:
        raise ValidationError("a link URL is required")
    if len(cleaned) > _MAX_URL_LEN:
        raise ValidationError(f"the URL must be at most {_MAX_URL_LEN} characters")
    if any(ch in _CONTROL_CHARS for ch in cleaned):
        raise ValidationError("the URL contains control characters")
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        raise ValidationError("the URL is malformed") from None
    if parts.scheme not in ("http", "https"):
        raise ValidationError("only http and https links are allowed")
    if not parts.hostname:
        raise ValidationError("the URL has no host")
    return cleaned


def create_link(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    kind: LinkKind,
    url: str,
    label: str | None = None,
    notes: str | None = None,
) -> Link:
    """Attach an external link to an entity (validates the URL and the target)."""
    model = entity_model(entity_type)
    require_entity(session, model, entity_id, entity_type)

    clean_url = _validate_url(url)
    label = label.strip() if label else None
    notes = notes.strip() if notes else None
    if label is not None and len(label) > _MAX_LABEL_LEN:
        raise ValidationError(f"the label must be at most {_MAX_LABEL_LEN} characters")
    if notes is not None and len(notes) > _MAX_NOTES_LEN:
        raise ValidationError(f"notes must be at most {_MAX_NOTES_LEN} characters")

    link = Link(
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        url=clean_url,
        label=label or None,
        notes=notes or None,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


def list_links(session: Session, *, entity_type: str, entity_id: int) -> list[Link]:
    """Return the links for one entity, oldest first.

    The target must exist (unknown type → 422, unknown id → 404), matching
    ``create_link`` rather than silently returning an empty list.
    """
    model = entity_model(entity_type)
    require_entity(session, model, entity_id, entity_type)
    statement = (
        select(Link)
        .where(Link.entity_type == entity_type)
        .where(Link.entity_id == entity_id)
        .order_by(col(Link.id))
    )
    return list(session.exec(statement).all())


def get_link(session: Session, link_id: int) -> Link:
    """Return a link row or raise :class:`NotFoundError`."""
    return require_entity(session, Link, link_id, "link")


def delete_link(session: Session, link_id: int) -> None:
    """Hard-delete a single link."""
    link = get_link(session, link_id)
    session.delete(link)
    session.commit()


def delete_links_for(session: Session, *, entity_type: str, entity_id: int) -> None:
    """Delete every link for a parent entity.

    Runs in the caller's transaction (no commit): a hard delete of a parent uses
    this so its links don't dangle, exactly as attachments do.
    """
    statement = (
        select(Link)
        .where(Link.entity_type == entity_type)
        .where(Link.entity_id == entity_id)
    )
    for link in session.exec(statement).all():
        session.delete(link)


__all__ = [
    "create_link",
    "delete_link",
    "delete_links_for",
    "get_link",
    "list_links",
]

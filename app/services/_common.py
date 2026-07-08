"""Shared helpers for the service layer."""

from __future__ import annotations

from sqlmodel import Session, SQLModel

from app.services.errors import NotFoundError


def require_entity[M: SQLModel](
    session: Session, model: type[M], entity_id: int, label: str
) -> M:
    """Fetch an entity by primary key or raise :class:`NotFoundError`."""
    entity = session.get(model, entity_id)
    if entity is None:
        raise NotFoundError(f"{label} {entity_id} not found")
    return entity

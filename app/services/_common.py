"""Shared helpers for the service layer."""

from __future__ import annotations

from sqlmodel import Session, SQLModel

from app.models.bom import Bom
from app.models.component import Component
from app.models.invoice import Invoice
from app.services.errors import NotFoundError, ValidationError

# Which entity a generic ``(entity_type, entity_id)`` pair points at. Shared by the
# attachment and link services so both attach to the same set of entities; anything
# else is rejected, so a row can never dangle off a type we don't recognise.
_ENTITY_MODELS: dict[str, type[SQLModel]] = {
    "component": Component,
    "invoice": Invoice,
    "bom": Bom,
}


def entity_model(entity_type: str) -> type[SQLModel]:
    """Return the model for a known ``entity_type`` or raise ``ValidationError``."""
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise ValidationError(f"unknown entity_type {entity_type!r}")
    return model


def require_entity[M: SQLModel](
    session: Session, model: type[M], entity_id: int, label: str
) -> M:
    """Fetch an entity by primary key or raise :class:`NotFoundError`."""
    entity = session.get(model, entity_id)
    if entity is None:
        raise NotFoundError(f"{label} {entity_id} not found")
    return entity

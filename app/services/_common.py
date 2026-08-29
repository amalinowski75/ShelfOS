"""Shared helpers for the service layer."""

from __future__ import annotations

import re
import unicodedata

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


def refuse_if_deleted(entity: SQLModel, name: str) -> None:
    """Refuse a write to something that has been taken out of use (§20).

    A soft delete keeps the row so that the records pointing at it keep meaning
    something -- not so that it can go on being written to. Whether a model has
    the notion at all is read off the row, so this covers every service that
    reaches an entity generically (attachments, links) as well as the component
    paths, and a second soft-deleted entity would get the rule rather than a
    second copy of it. The caller passes the best name it has, so the sentence
    says "RC0603" where it can and "component #7" where it cannot.
    """
    if getattr(entity, "deleted_at", None) is not None:
        raise ValidationError(f"{name} is deleted — restore it first.")


def require_entity[M: SQLModel](
    session: Session, model: type[M], entity_id: int, label: str
) -> M:
    """Fetch an entity by primary key or raise :class:`NotFoundError`."""
    entity = session.get(model, entity_id)
    if entity is None:
        raise NotFoundError(f"{label} {entity_id} not found")
    return entity


# Fold a shop's parameter label / value to a comparable key: lowercase, strip accents
# to their base letter, then drop every non-alphanumeric character. So "Rezystancja",
# "Resistance (Ω)" and "resistance" fold the same — and, crucially for Polish, so do
# "wstążkowy" and "wstazkowy" (an accent must not simply vanish and change the word).
_NON_ALNUM = re.compile(r"[^a-z0-9]")
# Letters NFKD does not decompose (they have no combining form), folded by hand.
_STANDALONE_FOLD = str.maketrans({"ł": "l", "đ": "d", "ø": "o", "ß": "ss", "þ": "th"})


def normalize(name: str | None) -> str:
    text = str(name or "").lower().translate(_STANDALONE_FOLD)
    # NFKD splits e.g. "ż" into "z" + a combining mark; dropping the marks leaves "z".
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return _NON_ALNUM.sub("", text)

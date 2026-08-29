"""Resolving a manufacturer name to the one spelling ShelfOS stores.

A component's identity is (MPN, manufacturer), and every source spells the maker
differently — so without this, ``ONSEMI`` and ``ON Semiconductor`` are two parts.

The rule is deliberately not clever. A name resolves ONLY through an alias the user
entered by picking an existing component; anything unrecognised is kept as it came.
``shops.base.manufacturer_matches`` exists and would resolve the easy cases for
free, but it is a comparator for choosing among a shop's own results, where the
alternative is arbitrary ordering. Applied to identity it merges real companies:
"Micro Commercial Components" and "Micro Crystal" both match "Microchip Technology",
because "micro" is a long-enough prefix of "microchip". A missed match costs a
duplicate; a false one silently fuses two different parts.
"""

from __future__ import annotations

from typing import cast

from sqlmodel import Session, col, select

from app.models.manufacturer import ManufacturerAlias
from app.services._common import normalize
from app.services.errors import ValidationError


def _blank_to_none(text: str | None) -> str | None:
    stripped = (text or "").strip()
    return stripped or None


def canonical_name(session: Session, name: str | None) -> str | None:
    """The spelling to store for ``name`` — its canonical one, or ``name`` itself.

    Comparison is on the normalised key (case, accents and punctuation folded), for
    the reason ``find_duplicate_component`` gives for normalising in Python rather
    than SQL: SQLite's own ``lower()`` is ASCII-only, so an accented maker would
    escape a DB-folded comparison.
    """
    wanted = _blank_to_none(name)
    if wanted is None:
        return None
    key = normalize(wanted)
    if not key:
        return wanted
    for alias in session.exec(select(ManufacturerAlias)).all():
        if normalize(alias.alias) == key:
            return alias.canonical
    return wanted


def list_aliases(session: Session) -> list[ManufacturerAlias]:
    """Every alias, canonical first then alias, for the admin listing."""
    return list(
        session.exec(
            select(ManufacturerAlias).order_by(
                col(ManufacturerAlias.canonical), col(ManufacturerAlias.alias)
            )
        ).all()
    )


def record_alias(
    session: Session, *, alias: str | None, canonical: str | None
) -> ManufacturerAlias | None:
    """Remember that ``alias`` means ``canonical``; returns None when there is
    nothing to record.

    Nothing to record covers two ordinary cases, neither of which is an error: the
    incoming name was blank (a Farnell invoice prints no manufacturer at all), or it
    already normalises to the canonical one — "ONSEMI" against "onsemi" is a spelling
    the lookup handles without help, and storing it would be noise.

    An alias already pointing somewhere else is REPOINTED rather than refused. It is
    the answer to "which existing part is this", so the newest answer is the best
    evidence; and only future lookups change, never a stored component.
    """
    canonical_text = _blank_to_none(canonical)
    if canonical_text is None:
        raise ValidationError("an alias needs a canonical manufacturer to point at")
    # One check for both "nothing to record" cases: a blank name has no characters
    # to normalise, so it folds to an empty key just as surely as a name that folds
    # to the canonical one. Spelling the blank case out separately reads clearer but
    # is unreachable — this line already returns for it.
    key = normalize(alias)
    if not key or key == normalize(canonical_text):
        return None
    # A non-empty key means the name had something in it.
    alias_text = cast(str, _blank_to_none(alias))

    existing = next(
        (row for row in session.exec(select(ManufacturerAlias)).all()
         if normalize(row.alias) == key),
        None,
    )
    if existing is not None:
        existing.alias = alias_text
        existing.canonical = canonical_text
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    row = ManufacturerAlias(alias=alias_text, canonical=canonical_text)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_alias(session: Session, alias_id: int) -> None:
    row = session.get(ManufacturerAlias, alias_id)
    if row is None:
        raise ValidationError("no such manufacturer alias")
    session.delete(row)
    session.commit()

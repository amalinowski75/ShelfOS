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
from app.services._common import normalize, require_entity
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

    Chains are collapsed HERE rather than followed at lookup time, in both
    directions: the target is resolved through any alias it is itself known by, and
    any row pointing at this alias is moved on to the same target. So recording
    "TI -> Texas Instr" and then "Texas Instr -> Texas Instruments" leaves TI
    pointing at "Texas Instruments" — where a one-pass lookup over un-collapsed rows
    would have answered "Texas Instr", a spelling nothing is filed under any more,
    and quietly started a third variant.
    """
    canonical_text = _blank_to_none(canonical)
    if canonical_text is None:
        raise ValidationError("an alias needs a canonical manufacturer to point at")
    # Follow the target's own alias, if it has one.
    canonical_text = cast(str, canonical_name(session, canonical_text))
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
        row = existing
    else:
        row = ManufacturerAlias(alias=alias_text, canonical=canonical_text)
    session.add(row)
    session.commit()
    session.refresh(row)
    # One collapse for both paths. It can only ever move something on the INSERT
    # path — after any write, no alias points at another alias, so by the time a
    # spelling has a row of its own nothing is left pointing at it — but writing
    # that as two calls would leave one of them permanently unreachable.
    _collapse_chains_into(session, key=key, canonical=canonical_text)
    return row


def _collapse_chains_into(session: Session, *, key: str, canonical: str) -> None:
    """Move every alias pointing at ``key`` on to ``canonical``.

    The other half of keeping the table one level deep — see ``record_alias``.
    """
    changed = False
    for row in session.exec(select(ManufacturerAlias)).all():
        if normalize(row.canonical) == key and row.canonical != canonical:
            row.canonical = canonical
            session.add(row)
            changed = True
    if changed:
        session.commit()


def agrees_with(canonical: str | None, stored: str | None) -> bool | None:
    """Whether a stated maker and a stored one are the same company.

    ``None`` — not ``False`` — whenever EITHER side named nobody. Silence is an
    unanswered question, not a contradiction, and the difference decides whether a
    part is offered or refused: most 1D barcodes carry no maker at all, and a
    Farnell invoice prints no manufacturer column, so every component that arrived
    on one has none stored. Reading either silence as disagreement refuses a whole
    class of perfectly good parts.

    The rule to check any change against: **a match stands unless the other side
    actively contradicts it.**

    ``canonical`` is expected to have been through :func:`canonical_name` already —
    resolve it once per request rather than once per candidate — so a bag or an
    invoice line saying ``ONSEMI`` is measured against the spelling the components
    are stored under.
    """
    if not canonical or not stored:
        return None
    return normalize(stored) == normalize(canonical)


def delete_alias(session: Session, alias_id: int) -> None:
    # NotFoundError, so the route answers 404 like every other missing entity. The
    # ordinary way to arrive here is two admins on the page at once: one forgets a
    # row, the other clicks Forget on a table that no longer matches the database.
    row = require_entity(session, ManufacturerAlias, alias_id, "manufacturer alias")
    session.delete(row)
    session.commit()

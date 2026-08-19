"""Editable matching rules for the enrichment engine.

The import/enrichment engine (``app/services/matching.py``) turns the free-text a shop
or invoice gives us — a category name, a mounting word, a parameter label, an enum
value — into structured ShelfOS fields. The vocabulary it recognises is NOT hardcoded:
it lives in this one table so it can grow (new synonyms, new languages) without a code
change. Each row says "when you see this ``alias``, treat it as this ``canonical``
value in this ``domain``".

Kept across ``reset_db --keep-types`` (it is taxonomy, like the type tree).
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from app.models.enums import MatchDomain, enum_column


class MatchRule(SQLModel, table=True):
    __tablename__ = "match_rules"

    id: int | None = Field(default=None, primary_key=True)
    domain: MatchDomain = Field(sa_column=enum_column(MatchDomain, index=True))
    # The free-text token to look for (stored as entered; matched case-insensitively).
    alias: str
    # The ShelfOS value it resolves to: a type name, a MountingType value, the package
    # text to store, a parameter definition's name, or one of that definition's
    # allowed enum values.
    canonical: str
    # NULL for the global domains (type, mounting, package); the owning definition for
    # the per-parameter domains (param_name, enum_value).
    parameter_definition_id: int | None = Field(
        default=None, foreign_key="parameter_definitions.id"
    )
    # Lower first: preserves specificity (led before diode, cable before connector, the
    # "ic:" catch-all last) exactly as the old hardcoded keyword list did.
    sort_order: int = Field(default=0)

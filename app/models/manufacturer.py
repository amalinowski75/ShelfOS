"""Manufacturer name aliases — one canonical spelling, many seen ones.

The same maker reaches ShelfOS spelled differently by every source: Farnell's API
says ``ONSEMI``, a Digi-Key invoice says ``ON Semiconductor``. Since a component's
identity is (MPN, manufacturer), the two used to become two components.

A row here says "this spelling means that maker". The canonical name is the one a
component STORES — aliases are only ever a way in, never something to display or
resolve at read time.

Rows are written by the user, one decision at a time: an import that finds the MPN
under a different spelling asks which existing part it is, and remembers the answer.
Nothing is inferred automatically, because a merge of two makers is silent and
wrong where a duplicate is merely noisy.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class ManufacturerAlias(SQLModel, table=True):
    __tablename__ = "manufacturer_aliases"

    id: int | None = Field(default=None, primary_key=True)
    # The spelling as it arrived, kept verbatim so the table reads like what the
    # user actually saw. Matching is done on a normalised key computed in Python
    # (see manufacturer_service), never on this column directly.
    alias: str = Field(index=True)
    # The spelling every component gets stored with.
    canonical: str

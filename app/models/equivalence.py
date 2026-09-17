"""Equivalence groups: several catalogue entries that are one physical part.

The same part reaches the shelves under more than one part number. Tape, tray and
loose bulk of one transistor carry different MPNs — the packaging suffix is part of
what you order — and a maker sometimes renumbers a part for reasons that have
nothing to do with the silicon inside it. Keeping them as separate components is
right: each one is a thing that can be ordered, received, priced and counted.

But a BOM line asks a different question. "How many of these do I have?" is
answered by the total across every entry that is the same part, and a line pointed
at one of them alone reads as short while the drawer next to it is full.

A group is where that fact is written down, once, for every BOM to read.

Two new tables and no column on ``components``. The schema has no migrations —
``create_all`` adds a missing TABLE to an existing database but never a missing
COLUMN — so a group reaches a running installation on the next restart, while a
``group_id`` on the component would reach only a database recreated from scratch.

Membership is unique per component, and that is what makes this a partition rather
than a web of pairs: "is the same part as" is transitive, so a component that is
the same as two others puts all three in one group. There is deliberately no way
to say that A matches B, B matches C, and A does not match C.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class ComponentEquivalenceGroup(SQLModel, table=True):
    """One physical part, however many catalogue entries it has.

    Carries no list of its own: the members point here, so adding or dropping a
    variant is one row and the group's identity survives it. A group that falls
    below two members is deleted by the service — "the same part as" needs
    something to be the same as, and a group of one says nothing.
    """

    __tablename__ = "component_equivalence_groups"

    id: int | None = Field(default=None, primary_key=True)
    # Why these entries are one part, in the words of whoever decided it ("tape
    # and bulk of the same die"). Optional, and nullable for the reason the module
    # docstring gives: a column added later would not reach an existing database,
    # so the place for the explanation has to exist before anyone asks for it.
    notes: str | None = Field(default=None)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ComponentEquivalenceMember(SQLModel, table=True):
    """One catalogue entry's place in a group.

    ``component_id`` is unique, not merely indexed: a component belongs to at most
    one group, which is the transitivity rule of the module docstring enforced by
    the database rather than by every caller remembering it.

    A component soft-deleted after it was grouped keeps its membership. Deletion is
    reversible (``restore_component``), and dropping the row would quietly lose a
    decision the restore could not bring back; the stock sums skip a deleted member
    instead, which is the same rule the BOM report already applies to an assignment
    whose part was retired.
    """

    __tablename__ = "component_equivalence_members"

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="component_equivalence_groups.id", index=True)
    component_id: int = Field(foreign_key="components.id", unique=True)

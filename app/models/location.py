"""Storage location and per-location stock models (spec §7-8).

``ComponentLocation.quantity`` is a materialized cache; the source of truth is
the stock-movement ledger (decision D1).
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.enums import ContainerType, LocationType, enum_column


class Location(SQLModel, table=True):
    __tablename__ = "locations"

    id: int | None = Field(default=None, primary_key=True)
    parent_id: int | None = Field(default=None, foreign_key="locations.id")
    type: LocationType = Field(sa_column=enum_column(LocationType))
    name: str


class ComponentLocation(SQLModel, table=True):
    __tablename__ = "component_locations"
    # One cache row per (component, location) slot -- the natural key. Prevents a
    # concurrent first movement from splitting a slot's quantity across two rows.
    __table_args__ = (
        UniqueConstraint(
            "component_id", "location_id", name="uq_component_location_slot"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    component_id: int = Field(foreign_key="components.id")
    location_id: int = Field(foreign_key="locations.id")
    quantity: int = Field(default=0)
    container_type: ContainerType = Field(
        default=ContainerType.LOOSE, sa_column=enum_column(ContainerType)
    )
    note: str | None = Field(default=None)

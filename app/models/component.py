"""Component, component type and parameter (EAV) models (spec §4-6, §20).

Component types form a hierarchy and *inherit* parameter definitions from their
ancestors (decision D3). Parameter values use a controlled EAV layout where the
value lives in exactly one typed column (decision D6).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.enums import (
    ComponentStatus,
    MountingType,
    ParameterDataType,
    enum_column,
)


class ComponentType(SQLModel, table=True):
    __tablename__ = "component_types"
    # Name unique within a parent (DATA_MODEL). NULL parents are not caught by
    # this constraint (NULL != NULL in SQL), so root-level duplicates are guarded
    # by a service-layer pre-check instead.
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_component_type_name_per_parent"),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str
    parent_id: int | None = Field(default=None, foreign_key="component_types.id")


class ParameterDefinition(SQLModel, table=True):
    __tablename__ = "parameter_definitions"
    # A parameter's technical key is unique within its type (DATA_MODEL).
    __table_args__ = (
        UniqueConstraint("type_id", "name", name="uq_parameter_name_per_type"),
    )

    id: int | None = Field(default=None, primary_key=True)
    type_id: int = Field(foreign_key="component_types.id")
    name: str
    label: str
    data_type: ParameterDataType = Field(sa_column=enum_column(ParameterDataType))
    unit: str | None = Field(default=None)
    is_filterable: bool = Field(default=False)
    is_table_column: bool = Field(default=False)
    sort_order: int = Field(default=0)


class ParameterEnumValue(SQLModel, table=True):
    """Allowed value for a ``data_type == enum`` parameter (decision D6)."""

    __tablename__ = "parameter_enum_values"

    id: int | None = Field(default=None, primary_key=True)
    parameter_definition_id: int = Field(foreign_key="parameter_definitions.id")
    value: str
    sort_order: int = Field(default=0)


class Component(SQLModel, table=True):
    __tablename__ = "components"

    id: int | None = Field(default=None, primary_key=True)
    type_id: int = Field(foreign_key="component_types.id")
    manufacturer: str | None = Field(default=None)
    mpn: str | None = Field(default=None, index=True)  # BOM import matches by MPN
    package: str | None = Field(default=None)
    mounting_type: MountingType = Field(
        default=MountingType.OTHER, sa_column=enum_column(MountingType)
    )
    notes: str | None = Field(default=None)
    status: ComponentStatus = Field(
        default=ComponentStatus.ACTIVE, sa_column=enum_column(ComponentStatus)
    )
    # Soft delete (spec §20).
    deleted_at: datetime | None = Field(default=None)
    deleted_reason: str | None = Field(default=None)
    deleted_by: int | None = Field(default=None, foreign_key="users.id")


class ComponentParameter(SQLModel, table=True):
    """One EAV value; exactly one ``value_*`` column is populated (decision D6)."""

    __tablename__ = "component_parameters"

    id: int | None = Field(default=None, primary_key=True)
    component_id: int = Field(foreign_key="components.id")
    parameter_definition_id: int = Field(foreign_key="parameter_definitions.id")
    value_num: float | None = Field(default=None)
    value_text: str | None = Field(default=None)
    value_bool: bool | None = Field(default=None)

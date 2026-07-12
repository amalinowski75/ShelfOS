"""Request body schemas for the API.

Response models reuse the SQLModel table classes directly (they are Pydantic
models), so only inbound payloads need dedicated schemas here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    ContainerType,
    LocationType,
    MountingType,
    ParameterDataType,
    StockReason,
)

# Order matters for Pydantic union coercion: bool before int (bool is an int
# subclass) so JSON ``true`` stays a bool rather than becoming ``1``.
ParameterValue = bool | int | float | str


class ParameterDefinitionCreate(BaseModel):
    name: str
    label: str
    data_type: ParameterDataType
    unit: str | None = None
    is_filterable: bool = False
    is_table_column: bool = False
    sort_order: int = 0
    enum_values: list[str] | None = None


class ParameterDefinitionRead(BaseModel):
    """A parameter definition plus its allowed enum tokens (spec §6, §13).

    ``enum_values`` lists the choices for an ``enum`` parameter in display order
    so a client can render a picker without a second call; it is an empty list
    for every non-enum ``data_type``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    type_id: int
    name: str
    label: str
    data_type: ParameterDataType
    unit: str | None
    is_filterable: bool
    is_table_column: bool
    sort_order: int
    enum_values: list[str] = Field(default_factory=list)


class TypeCreate(BaseModel):
    name: str
    parent_id: int | None = None
    # Optional parameter definitions created atomically with the type (§13).
    parameters: list[ParameterDefinitionCreate] = Field(default_factory=list)


class TypeWithParameters(BaseModel):
    """A created type plus the parameter definitions it directly owns (§13)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: int | None
    parameters: list[ParameterDefinitionRead]


class ComponentCreate(BaseModel):
    type_id: int
    manufacturer: str | None = None
    mpn: str | None = None
    package: str | None = None
    mounting_type: MountingType = MountingType.OTHER
    notes: str | None = None


class ParameterValueSet(BaseModel):
    parameter_definition_id: int
    value: ParameterValue


class LocationCreate(BaseModel):
    type: LocationType
    name: str
    parent_id: int | None = None


class StockAdd(BaseModel):
    component_id: int
    location_id: int
    quantity: int
    # None leaves an existing slot's container type untouched (new slots default
    # to LOOSE); a concrete value sets it on the slot.
    container_type: ContainerType | None = None
    reason: StockReason = StockReason.PURCHASE
    note: str | None = None


class StockRemove(BaseModel):
    component_id: int
    location_id: int
    quantity: int
    reason: StockReason = StockReason.USAGE
    note: str | None = None


class StockCorrection(BaseModel):
    component_id: int
    location_id: int
    delta: int
    note: str | None = None


class InvoiceCreate(BaseModel):
    supplier: str
    invoice_number: str
    invoice_date: date
    currency: str
    notes: str | None = None
    file_path: str | None = None


class InvoiceLineCreate(BaseModel):
    component_id: int
    quantity: int
    unit_price: Decimal
    supplier_part_number: str | None = None
    location_id: int | None = None


class InvoiceLineComponentRead(BaseModel):
    """Identity of the component a line refers to (invoice → component nav, §9)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    manufacturer: str | None
    mpn: str | None
    type_id: int


class InvoiceLineRead(BaseModel):
    """An invoice line with its referenced component resolved."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_id: int
    component_id: int
    supplier_part_number: str | None
    quantity: int
    unit_price: Decimal
    total_price: Decimal
    location_id: int | None
    # ``None`` when the referenced component was hard-deleted (§20 keeps the
    # line as history); ``component_id`` above still records the original id.
    component: InvoiceLineComponentRead | None


class InvoiceDetailRead(BaseModel):
    """An invoice header, its totals and its lines (spec §16)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier: str
    invoice_number: str
    invoice_date: date
    currency: str
    total_net: Decimal
    total_gross: Decimal
    file_path: str | None
    notes: str | None
    is_finalized: bool
    lines: list[InvoiceLineRead]


class LineLocationSet(BaseModel):
    location_id: int


class InvoiceFinalize(BaseModel):
    total_gross: Decimal | None = None

"""SQLModel database models.

Importing this package registers every table on ``SQLModel.metadata`` so that
``SQLModel.metadata.create_all(engine)`` creates the full schema.
"""

from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.bom import Bom, BomLine
from app.models.component import (
    Component,
    ComponentParameter,
    ComponentType,
    ParameterDefinition,
    ParameterEnumValue,
)
from app.models.enums import (
    AttachmentKind,
    ComponentStatus,
    ContainerType,
    LinkKind,
    LocationType,
    MatchDomain,
    MountingType,
    ParameterDataType,
    StockReason,
    UserRole,
)
from app.models.invoice import Invoice, InvoiceImportLine, InvoiceLine
from app.models.link import Link
from app.models.location import ComponentLocation, Location
from app.models.match_rule import MatchRule
from app.models.stock import StockMovement
from app.models.user import User

__all__ = [
    "Attachment",
    "AttachmentKind",
    "AuditLog",
    "Bom",
    "BomLine",
    "Component",
    "ComponentLocation",
    "ComponentParameter",
    "ComponentStatus",
    "ComponentType",
    "ContainerType",
    "Invoice",
    "InvoiceImportLine",
    "InvoiceLine",
    "Link",
    "LinkKind",
    "Location",
    "LocationType",
    "MatchDomain",
    "MatchRule",
    "MountingType",
    "ParameterDataType",
    "ParameterDefinition",
    "ParameterEnumValue",
    "StockMovement",
    "StockReason",
    "User",
    "UserRole",
]

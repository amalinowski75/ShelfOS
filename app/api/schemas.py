"""Request body schemas for the API.

Response models reuse the SQLModel table classes directly (they are Pydantic
models), so only inbound payloads need dedicated schemas here.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AttachmentKind,
    ContainerType,
    LinkKind,
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


class ParameterValueSet(BaseModel):
    parameter_definition_id: int
    value: ParameterValue


class ComponentCreate(BaseModel):
    type_id: int
    manufacturer: str | None = None
    mpn: str | None = None
    package: str | None = None
    mounting_type: MountingType = MountingType.OTHER
    notes: str | None = None
    # Optional initial parameter values, applied atomically with the component
    # (§16.5). Each definition must belong to the type's effective set.
    parameters: list[ParameterValueSet] = Field(default_factory=list)


class ParameterValueEdit(BaseModel):
    parameter_definition_id: int
    # A null (or blank) value clears the parameter; otherwise it is set.
    value: ParameterValue | None = None


class ComponentUpdate(BaseModel):
    """Admin edit of a component (§12). Type and MPN are immutable — omitted here so
    they can't be changed; the scalar fields and parameter values are replaced.

    The scalar fields are REQUIRED (no defaults): this replaces the full editable
    set, so an omitted field is a 422, never a silent wipe. A field may still be
    explicitly ``null`` to clear it. ``parameters`` is the full effective set the
    dialog renders; an empty list leaves parameters untouched.
    """

    manufacturer: str | None
    package: str | None
    mounting_type: MountingType
    notes: str | None
    parameters: list[ParameterValueEdit] = Field(default_factory=list)


class LocationCreate(BaseModel):
    type: LocationType
    name: str
    parent_id: int | None = None


class LocationUpdate(BaseModel):
    """Partial edit of a location; omitted fields stay unchanged.

    An explicit ``parent_id: null`` moves the location to the top level, so the
    route must pass only the fields the client actually sent (``exclude_unset``).
    """

    name: str | None = None
    parent_id: int | None = None
    type: LocationType | None = None


class LocationBulkLevel(BaseModel):
    """One level of a generated hierarchy (spec §7): ``count`` children of
    ``type`` under every location of the level above. ``{n}`` in the pattern is
    the child's number; ``None`` falls back to ``"<Type> {n}"``."""

    type: LocationType
    count: int = Field(ge=1, le=100)
    name_pattern: str | None = None


class LocationBulkCreate(BaseModel):
    levels: list[LocationBulkLevel] = Field(min_length=1, max_length=8)
    parent_id: int | None = None
    # True previews totals and sample paths without creating anything.
    dry_run: bool = False


class LabelPrintRequest(BaseModel):
    """Which location labels to send to the label printer (spec §7).

    ``root`` prints a location and everything under it; ``ids`` prints exactly
    those, in that order; neither prints every location. Ids are bounded by
    SQLite's rowid range — past it the driver raises mid-query, which would be
    an unmapped 500 rather than a 422.
    """

    ids: list[Annotated[int, Field(gt=0, le=2**63 - 1)]] | None = None
    root: Annotated[int, Field(gt=0, le=2**63 - 1)] | None = None
    copies: int = Field(default=1, ge=1, le=10)
    # The roll to print on. Omitted, the printer's own tape decides; named, a
    # printer holding something else answers 409 with both tapes rather than
    # printing, unless ``accept_loaded`` settles it in advance.
    tape: str | None = None
    accept_loaded: bool = False


class TapeRead(BaseModel):
    """A tape the printer can be asked to print on."""

    id: str
    name: str
    width_mm: int
    length_mm: int | None
    two_color: bool


class TapesRead(BaseModel):
    """The tapes on offer, plus which one is configured and which is loaded."""

    tapes: list[TapeRead]
    configured: str
    # What the printer says it is holding, or null when it is not answering.
    loaded: str | None


class LabelPrintResult(BaseModel):
    """How many labels went to the printer, and whether it confirmed printing.

    A QL answers questions, so a job usually comes back confirmed. ``confirmed``
    is false when the printer stayed silent — an unusual firmware, or a device
    that is not really a printer — and the caller should then say a job was
    *sent* rather than printed.
    """

    sent: int
    confirmed: bool
    # Which tape it went on. Normally the one the printer reports holding, which
    # is not necessarily the configured one — the layout follows the machine.
    tape: str
    # True when the run was stopped part-way, so ``sent`` is not the whole job.
    stopped: bool = False


class LabelJobRead(BaseModel):
    """How far a running print has got, for a caller watching it."""

    printing: bool
    done: int
    total: int


class LocationBulkResult(BaseModel):
    """Outcome of a bulk generation; ``created`` is 0 for a dry run."""

    total: int
    created: int
    sample_paths: list[str]


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


class StockMove(BaseModel):
    """Relocate stock; ``quantity: null`` moves everything the source holds."""

    component_id: int
    from_location_id: int
    to_location_id: int
    quantity: int | None = Field(default=None, gt=0)
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


class InvoiceUpdate(BaseModel):
    """Partial edit of a draft invoice's metadata; omitted fields stay unchanged."""

    supplier: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    currency: str | None = None
    notes: str | None = None
    file_path: str | None = None


class InvoiceLineUpdate(BaseModel):
    """Partial edit of a draft invoice line; omitted fields stay unchanged."""

    quantity: int | None = None
    unit_price: Decimal | None = None
    supplier_part_number: str | None = None


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


class InvoiceImportResult(BaseModel):
    """Outcome of a PDF import: the draft invoice and how its lines landed."""

    invoice_id: int
    added: int
    pending: int


class InvoiceImportLineRead(BaseModel):
    """A staged import line under review on the draft invoice page.

    ``type_id`` set + ``location_id`` set = ready to be created at finalize.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_id: int
    line_no: int
    supplier_part_number: str | None
    mpn: str | None
    manufacturer: str | None
    description: str | None
    package: str | None
    quantity: int
    unit_price: Decimal
    shop_key: str
    type_id: int | None
    location_id: int | None
    mounting_type: MountingType
    parameters: list[ParameterValueSet]
    reason: str


class InvoiceImportLineUpdate(BaseModel):
    """Review edits to a staged import line; only the fields sent are changed.

    ``type_id`` set marks the row ready; ``null`` sends it back to needs-review.
    Changing ``type_id`` clears any ``parameters`` (they belong to a type). The
    identity fields and parameters seed the component created at finalize.
    """

    type_id: int | None = None
    location_id: int | None = None
    manufacturer: str | None = None
    mpn: str | None = None
    package: str | None = None
    mounting_type: MountingType | None = None
    description: str | None = None
    parameters: list[ParameterValueSet] | None = None
    # What the bag actually holds, when that differs from the invoice.
    quantity: int | None = Field(default=None, gt=0)


class AttachmentRead(BaseModel):
    """Attachment metadata for the API.

    Omits ``file_path`` so the internal on-disk name/layout never leaks; the file
    is reached only through the download endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    kind: AttachmentKind
    filename: str
    notes: str | None


class AttachmentFromUrl(BaseModel):
    """Attach a file fetched server-side from a public URL (spec §10)."""

    entity_type: str
    entity_id: int
    url: str = Field(max_length=2048)
    kind: AttachmentKind = AttachmentKind.OTHER
    notes: str | None = None


class LinkCreate(BaseModel):
    """Attach an external clickable URL to an entity (a link, not a stored file)."""

    entity_type: str
    entity_id: int
    kind: LinkKind = LinkKind.OTHER
    url: str = Field(max_length=2048)
    label: str | None = None
    notes: str | None = None


class LinkUpdate(BaseModel):
    """Edit a link's kind/url/label/notes (the target entity is immutable).

    A full replace of the editable fields — the edit dialog always sends the whole
    row — so a blank label/notes clears it. ``kind`` and ``url`` are required (no
    defaults) so the schema itself says "send the whole row": a partial body that
    omits ``kind`` is rejected rather than silently recategorising the link.
    """

    kind: LinkKind
    url: str = Field(max_length=2048)
    label: str | None = None
    notes: str | None = None


class LinkRead(BaseModel):
    """External-link metadata for the API.

    Unlike ``AttachmentRead``, the ``url`` IS returned — a link is the URL, and the
    client renders it as a clickable anchor.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    kind: LinkKind
    url: str
    label: str | None
    notes: str | None


class ShopLookup(BaseModel):
    """Look a product up from a shop URL or a scanned barcode/QR payload."""

    code: str = Field(max_length=2048)


class ScanParseRead(BaseModel):
    """A scanned supplier label decoded to its identifiers — no shop API call.

    Everything a client needs to match the scan against data it already has
    (e.g. a draft invoice's lines); ``url`` covers a TME QR, whose payload is a
    product URL rather than identifier fields.
    """

    mpn: str | None = None
    manufacturer: str | None = None
    distributor_pn: str | None = None
    shop: str | None = None
    manufacturer_pn: str | None = None
    url: str | None = None
    # Symbol candidates read out of ``url`` (TME puts its own symbol in the
    # path, at no fixed position) — the only identifiers a URL-only QR offers.
    url_symbols: list[str] = Field(default_factory=list)


class ScannedStockRead(BaseModel):
    """Where a scanned component currently sits, and how much of it."""

    id: int
    path: str
    quantity: int


class ScannedComponentRead(BaseModel):
    """A component a scan resolved to, with the stock a relocation would move."""

    id: int
    mpn: str | None
    manufacturer: str | None
    description: str | None
    locations: list[ScannedStockRead]


class ComponentScanRead(BaseModel):
    """What a scanned bag resolves to: the identifiers read, and the matches.

    ``matches`` is empty when nothing in the inventory carries the part number,
    and may hold several — MPN is not unique — which the caller must not guess
    between.
    """

    identifiers: list[str]
    matches: list[ScannedComponentRead]


class ShopParameter(BaseModel):
    name: str
    value: str


class MatchProposalRead(BaseModel):
    """What the matching engine worked out for the dialog to apply, all reviewable."""

    type_id: int | None = None
    mounting_type: MountingType | None = None
    package: str | None = None
    # Values keyed by definition id (already gated), ready for the dialog's fields.
    parameters: list[ParameterValueSet] = Field(default_factory=list)


class MatchProposalRequest(BaseModel):
    """Re-run the engine for a chosen type (the dialog calls this on a type change)."""

    type_id: int | None = None
    category: str | None = None
    shop_category: str | None = None
    description: str | None = None
    package: str | None = None
    parameters: list[ShopParameter] = Field(default_factory=list)


class ShopProductRead(BaseModel):
    """A distributor product normalised toward the New Component dialog's fields."""

    category: str | None = None
    # The shop's own category text; the dialog mines it for facts the description
    # leaves out (mounting, case size). Distinct from `category`, which is already
    # resolved to a ShelfOS type name.
    shop_category: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    package: str | None = None
    datasheet_url: str | None = None
    parameters: list[ShopParameter] = Field(default_factory=list)
    # The product page the import resolved to, saved as the component's shop link.
    # Echoed back because a scan's URL is buried in the code the client sent.
    source_url: str | None = None
    # True when only the scanned label could be read — the shop's API added nothing.
    from_label_only: bool = False
    # The engine's proposal (type/mounting/package/parameters) for the dialog to apply.
    proposal: MatchProposalRead = Field(default_factory=MatchProposalRead)


class BomLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    references: str
    reference_prefix: str | None
    category: str | None
    value: str | None
    footprint: str | None
    mpn: str | None
    manufacturer: str | None
    quantity: int


class BomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    source_filename: str | None
    created_at: datetime


class BomDetailRead(BomRead):
    lines: list[BomLineRead]

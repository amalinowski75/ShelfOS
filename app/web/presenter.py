"""Presentation helpers for the web UI.

Builds the component-table payload (columns + rows) consumed by Tabulator and
formats EAV values for display, including engineering-notation numbers
(decision D4). Kept separate from the routes so it can be unit-tested.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from sqlmodel import Session, col, select

from app.models.component import ComponentParameter, ComponentType, ParameterDefinition
from app.models.enums import ParameterDataType
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import stock_service as ss
from app.units import format_engineering

# Columns shown for every component regardless of type (spec §11).
_BASE_COLUMNS: list[dict[str, object]] = [
    {"title": "Type", "field": "type"},
    {"title": "Manufacturer", "field": "manufacturer"},
    {"title": "MPN", "field": "mpn"},
    # The component's own free text (`notes`), which is also where a shop import
    # puts the manufacturer's product description — so it reads as the part's
    # description and is titled that way, next to the MPN it describes.
    {"title": "Description", "field": "notes"},
    {"title": "Package", "field": "package"},
    {"title": "Mounting", "field": "mounting_type"},
    {"title": "Qty", "field": "quantity"},
]


# Longest description shipped in a table row. `notes` is uncapped free text, and
# this feed is fetched on every load of the components page AND by the invoice
# line dialog, so one component with a novel in it would be downloaded in full
# every time. The detail page has the whole thing.
#
# This also bounds what the table's Description filter can find: that filter runs
# CLIENT-side over what this ships, so text past the cut is unsearchable there
# while the detail page still shows it. Rare at 200 characters, and the honest fix
# if it ever bites is server-side filtering for that column — not a fatter payload.
_TABLE_NOTES_CHARS = 200


def _short(text: str | None) -> str:
    """A description trimmed to table length, with an ellipsis when it was cut."""
    value = (text or "").strip()
    if len(value) <= _TABLE_NOTES_CHARS:
        return value
    return value[:_TABLE_NOTES_CHARS].rstrip() + "…"


def format_money(amount: Decimal) -> str:
    """Render a money ``Decimal`` for display without noisy trailing zeros.

    Amounts are stored at six decimal places (D5), so a plain ``str`` prints
    ``"1.500000"``. Drop insignificant trailing zeros but keep at least two
    decimals — money always reads with cents (``"1.50"``, ``"0.00"``) while a
    genuinely finer price (``"0.001234"``) stays exact.
    """
    # ``normalize`` can yield exponent notation (Decimal('1E+2')), but the ``f``
    # format expands it back to plain fixed-point ("100"), so no reinflation is
    # needed here.
    text = f"{amount.normalize():f}"
    integer, _, fraction = text.partition(".")
    if len(fraction) < 2:
        fraction = fraction.ljust(2, "0")
    return f"{integer}.{fraction}"


def format_parameter_value(
    definition: ParameterDefinition, param: ComponentParameter | None
) -> str:
    """Render an EAV value for display, or ``""`` when unset."""
    if param is None:
        return ""
    match definition.data_type:
        case ParameterDataType.NUMBER:
            if param.value_num is None:
                return ""
            return format_engineering(param.value_num, definition.unit or "")
        case ParameterDataType.BOOL:
            if param.value_bool is None:
                return ""
            return "yes" if param.value_bool else "no"
        case _:  # TEXT and ENUM both live in value_text.
            return param.value_text or ""


def build_invoice_table(session: Session, limit: int) -> dict[str, Any]:
    """Return ``{"data": [...], "truncated": bool, "limit": int}`` for the
    Tabulator invoice list (newest first, §16).

    Money is pre-formatted here so the exact ``Decimal`` amounts (D5) never round
    through JavaScript; the client only sorts, filters and renders the strings.
    A draft has no gross yet, so its gross reads ``"—"``. ``truncated`` says the
    list hit ``limit`` and older invoices are hidden.
    """
    # Fetch one past the cap so we can tell "exactly `limit` rows exist" (nothing
    # hidden) from "more than `limit` exist" (older ones dropped), then show only
    # the first `limit`.
    invoices = inv.list_invoices(session, limit=limit + 1)
    truncated = len(invoices) > limit
    invoices = invoices[:limit]
    data = [
        {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "supplier": invoice.supplier,
            "invoice_date": invoice.invoice_date.isoformat(),
            "net": f"{format_money(invoice.total_net)} {invoice.currency}",
            "gross": (
                f"{format_money(invoice.total_gross)} {invoice.currency}"
                if invoice.is_finalized
                else "—"
            ),
            "status": "finalized" if invoice.is_finalized else "draft",
        }
        for invoice in invoices
    ]
    return {"data": data, "truncated": truncated, "limit": limit}


def build_component_table(
    session: Session, type_id: int | None = None
) -> dict[str, Any]:
    """Return ``{"columns": [...], "data": [...]}`` for the component table.

    In the generic view only common columns are returned; when a single type is
    selected, its table-flagged parameters are appended as extra columns (§11).
    """
    columns: list[dict[str, object]] = list(_BASE_COLUMNS)
    table_params: list[ParameterDefinition] = []
    if type_id is not None:
        table_params = [
            d
            for d in cs.get_effective_parameter_definitions(session, type_id)
            if d.is_table_column
        ]
        # `numeric` tells the client to sort this column by the raw value the
        # rows carry (below), not the engineering-formatted display string.
        columns += [
            {
                "title": d.label,
                "field": f"param_{d.id}",
                "numeric": d.data_type is ParameterDataType.NUMBER,
            }
            for d in table_params
        ]

    totals = ss.total_quantities_by_component(session)
    components = cs.list_components(session, type_id=type_id)

    # Preload type names and (when needed) parameter values in one query each,
    # instead of a per-row lookup, so the table scales past demo size.
    type_names = {t.id: t.name for t in session.exec(select(ComponentType)).all()}
    values_by_component = _load_parameter_values(
        session, [cast(int, c.id) for c in components] if table_params else []
    )

    rows: list[dict[str, Any]] = []
    for component in components:
        component_id = cast(int, component.id)
        row: dict[str, Any] = {
            "id": component_id,
            "type": type_names.get(component.type_id, ""),
            "manufacturer": component.manufacturer or "",
            "mpn": component.mpn or "",
            "notes": _short(component.notes),
            "package": component.package or "",
            "mounting_type": component.mounting_type.value,
            "quantity": totals.get(component_id, 0),
        }
        if table_params:
            values = values_by_component.get(component_id, {})
            for definition in table_params:
                param = values.get(cast(int, definition.id))
                field = f"param_{definition.id}"
                row[field] = format_parameter_value(definition, param)
                if definition.data_type is ParameterDataType.NUMBER:
                    # Raw value beside the formatted string so the client sorts
                    # the column by magnitude (47 Ω < 220 Ω < 1 kΩ), not text.
                    row[f"{field}__n"] = (
                        param.value_num if param is not None else None
                    )
        rows.append(row)

    return {"columns": columns, "data": rows}


def _load_parameter_values(
    session: Session, component_ids: list[int]
) -> dict[int, dict[int, ComponentParameter]]:
    """Return ``{component_id: {definition_id: value}}`` in a single query."""
    if not component_ids:
        return {}
    grouped: dict[int, dict[int, ComponentParameter]] = {}
    for param in session.exec(
        select(ComponentParameter).where(
            col(ComponentParameter.component_id).in_(component_ids)
        )
    ).all():
        grouped.setdefault(param.component_id, {})[
            param.parameter_definition_id
        ] = param
    return grouped


def build_location_stock(session: Session) -> dict[int, list[dict[str, Any]]]:
    """``{location_id: [{component_id, mpn, manufacturer, quantity, container}]}``.

    What the locations page shows *inside* each location. Built from two queries
    (the slots, then the components they name) rather than a lookup per node, and
    sorted by MPN so a drawer's contents read in a stable order.
    """
    from app.models.component import Component

    by_location = ss.stock_by_location(session)
    component_ids = {
        slot.component_id for slots in by_location.values() for slot in slots
    }
    if not component_ids:
        return {}
    components = {
        cast(int, c.id): c
        for c in session.exec(
            select(Component).where(col(Component.id).in_(component_ids))
        ).all()
    }

    contents: dict[int, list[dict[str, Any]]] = {}
    for location_id, slots in by_location.items():
        rows = []
        for slot in slots:
            component = components.get(slot.component_id)
            if component is None:  # pragma: no cover - FK makes this unreachable
                continue
            rows.append(
                {
                    "component_id": slot.component_id,
                    # Never blank: a component may have no MPN, and an empty link
                    # would be unclickable.
                    "mpn": component.mpn or f"Component #{slot.component_id}",
                    "manufacturer": component.manufacturer or "",
                    "quantity": slot.quantity,
                    "container": slot.container_type.value,
                }
            )
        rows.sort(key=lambda row: str(row["mpn"]).casefold())
        contents[location_id] = rows
    return contents

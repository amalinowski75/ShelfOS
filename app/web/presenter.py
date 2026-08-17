"""Presentation helpers for the web UI.

Builds the component-table payload (columns + rows) consumed by Tabulator and
formats EAV values for display, including engineering-notation numbers
(decision D4). Kept separate from the routes so it can be unit-tested.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.component import (
    Component,
    ComponentParameter,
    ComponentType,
    ParameterDefinition,
)
from app.models.enums import ParameterDataType
from app.models.invoice import InvoiceImportLine
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

    Note this is the WHOLE inventory: ``_PARTS_PER_LOCATION`` bounds how much of one
    location gets rendered, not how much is loaded here, and nothing bounds the
    number of locations. Fine at a workshop's scale; the thing to reach for if it
    ever isn't is fetching a location's contents when it is first expanded.
    """
    from app.models.component import Component

    by_location = ss.stock_by_location(session)
    component_ids = {
        slot.component_id for slots in by_location.values() for slot in slots
    }
    if not component_ids:
        return {}
    # Three columns, not whole entities: a Component drags `notes` along, which is
    # uncapped free text that #59 went to some trouble NOT to ship where nobody
    # reads it. Nobody reads it here either.
    named = {
        component_id: (mpn, manufacturer)
        for component_id, mpn, manufacturer in session.exec(
            select(
                Component.id, Component.mpn, Component.manufacturer
            ).where(col(Component.id).in_(component_ids))
        ).all()
    }

    contents: dict[int, list[dict[str, Any]]] = {}
    for location_id, slots in by_location.items():
        rows = []
        for slot in slots:
            mpn, manufacturer = named.get(slot.component_id, (None, None))
            rows.append(
                {
                    "component_id": slot.component_id,
                    # Never blank: a component may have no MPN, and an empty link
                    # would be unclickable.
                    "mpn": mpn or f"Component #{slot.component_id}",
                    "manufacturer": manufacturer or "",
                    "quantity": slot.quantity,
                    "container": slot.container_type.value,
                }
            )
        rows.sort(key=lambda row: str(row["mpn"]).casefold())
        contents[location_id] = rows
    return contents


def build_types_table(session: Session) -> list[dict[str, Any]]:
    """Rows for the admin ``/types`` management page (§13 edit).

    Each type with its parent name, live-component and child counts, and its own
    parameter definitions (with enum tokens and a per-parameter in-use count). All
    counts are batched to avoid an N+1. The ``deletable`` flags mirror what the DELETE
    endpoints block on — components, child types AND staged invoice lines for a type;
    stored values AND staged lines for a parameter — so the page can disable a Delete
    the server would refuse. They stay advisory: the endpoints re-check and are the
    source of truth.
    """
    types = cs.list_types(session)
    name_by_id = {t.id: t.name for t in types}

    component_counts: dict[int, int] = dict(
        session.exec(
            select(Component.type_id, func.count())
            .where(col(Component.deleted_at).is_(None))
            .group_by(col(Component.type_id))
        ).all()
    )
    child_counts: dict[int, int] = {
        parent_id: n
        for parent_id, n in session.exec(
            select(ComponentType.parent_id, func.count()).group_by(
                col(ComponentType.parent_id)
            )
        ).all()
        if parent_id is not None
    }
    defs_by_type: dict[int, list[ParameterDefinition]] = {}
    all_defs = session.exec(
        select(ParameterDefinition).order_by(
            col(ParameterDefinition.type_id),
            col(ParameterDefinition.sort_order),
            col(ParameterDefinition.id),
        )
    ).all()
    for definition in all_defs:
        defs_by_type.setdefault(definition.type_id, []).append(definition)
    enum_values = cs.enum_values_by_definition(
        session, [cast(int, d.id) for d in all_defs]
    )
    usage: dict[int, int] = dict(
        session.exec(
            select(ComponentParameter.parameter_definition_id, func.count()).group_by(
                col(ComponentParameter.parameter_definition_id)
            )
        ).all()
    )
    # Staged invoice-import lines also block a delete (added in #79 for types, #80 for
    # parameters). type_id is a real FK (batched here); the definition ids live in the
    # parameters JSON, so that side is a small Python scan of the review panel.
    staged_type_counts: dict[int, int] = {
        type_id: n
        for type_id, n in session.exec(
            select(InvoiceImportLine.type_id, func.count()).group_by(
                col(InvoiceImportLine.type_id)
            )
        ).all()
        if type_id is not None
    }
    staged_def_ids: set[int] = set()
    for line in session.exec(select(InvoiceImportLine)).all():
        for entry in line.parameters or []:
            if isinstance(entry, dict) and isinstance(
                entry.get("parameter_definition_id"), int
            ):
                staged_def_ids.add(entry["parameter_definition_id"])

    rows: list[dict[str, Any]] = []
    for ctype in types:
        components = component_counts.get(cast(int, ctype.id), 0)
        children = child_counts.get(cast(int, ctype.id), 0)
        staged = staged_type_counts.get(cast(int, ctype.id), 0)
        parameters = [
            {
                "id": d.id,
                "name": d.name,
                "label": d.label,
                "data_type": d.data_type.value,
                "unit": d.unit,
                "sort_order": d.sort_order,
                "is_table_column": d.is_table_column,
                "is_filterable": d.is_filterable,
                "enum_values": enum_values.get(cast(int, d.id), []),
                "in_use_count": usage.get(cast(int, d.id), 0),
                "deletable": (
                    usage.get(cast(int, d.id), 0) == 0
                    and cast(int, d.id) not in staged_def_ids
                ),
            }
            for d in defs_by_type.get(cast(int, ctype.id), [])
        ]
        rows.append(
            {
                "id": ctype.id,
                "name": ctype.name,
                "parent_name": (
                    name_by_id.get(ctype.parent_id) if ctype.parent_id else None
                ),
                "component_count": components,
                "child_count": children,
                "deletable": components == 0 and children == 0 and staged == 0,
                "parameters": parameters,
            }
        )
    return rows

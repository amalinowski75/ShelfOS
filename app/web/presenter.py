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
from app.services import stock_service as ss
from app.units import format_engineering

# Columns shown for every component regardless of type (spec §11).
_BASE_COLUMNS: list[dict[str, str]] = [
    {"title": "Type", "field": "type"},
    {"title": "Manufacturer", "field": "manufacturer"},
    {"title": "MPN", "field": "mpn"},
    {"title": "Package", "field": "package"},
    {"title": "Mounting", "field": "mounting_type"},
    {"title": "Qty", "field": "quantity"},
]


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


def build_component_table(
    session: Session, type_id: int | None = None
) -> dict[str, Any]:
    """Return ``{"columns": [...], "data": [...]}`` for the component table.

    In the generic view only common columns are returned; when a single type is
    selected, its table-flagged parameters are appended as extra columns (§11).
    """
    columns: list[dict[str, str]] = list(_BASE_COLUMNS)
    table_params: list[ParameterDefinition] = []
    if type_id is not None:
        table_params = [
            d
            for d in cs.get_effective_parameter_definitions(session, type_id)
            if d.is_table_column
        ]
        columns += [{"title": d.label, "field": f"param_{d.id}"} for d in table_params]

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
            "package": component.package or "",
            "mounting_type": component.mounting_type.value,
            "quantity": totals.get(component_id, 0),
        }
        if table_params:
            values = values_by_component.get(component_id, {})
            for definition in table_params:
                row[f"param_{definition.id}"] = format_parameter_value(
                    definition, values.get(cast(int, definition.id))
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

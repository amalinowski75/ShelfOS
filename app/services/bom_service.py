"""KiCad BOM import + availability report (spec §21/§22).

Parse an uploaded KiCad BOM CSV into ``Bom``/``BomLine`` rows (the original file
is kept as a ``bom`` attachment), and compute — live against current stock — a
report of what is in inventory, what is short, and value-based substitute
suggestions for simple passives (R/C/L). No inventory is modified.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import cast

from sqlmodel import Session, col, select

from app import config
from app.models.bom import Bom, BomLine, BomLineAssignment
from app.models.component import (
    Component,
    ComponentParameter,
    ComponentType,
    ParameterDefinition,
)
from app.models.enums import AttachmentKind, ParameterDataType
from app.services import attachment_service, link_service
from app.services import component_service as cs
from app.services import stock_service as ss
from app.services._common import require_entity
from app.services.errors import NotFoundError, ValidationError
from app.units import UnitParseError, format_engineering, parse_engineering

# Reference-designator prefix → component category (standard KiCad conventions).
_PREFIX_CATEGORY: dict[str, str] = {
    "R": "resistor", "C": "capacitor", "L": "inductor", "FB": "ferrite",
    "D": "diode", "LED": "led", "Q": "transistor", "T": "transistor",
    "U": "ic", "IC": "ic", "J": "connector", "P": "connector", "CN": "connector",
    "SW": "switch", "S": "switch", "Y": "crystal", "X": "crystal",
    "K": "relay", "F": "fuse", "BR": "bridge",
}

# Categories whose value is a scalar we can match/substitute by (R/C/L).
_VALUE_CATEGORIES = {"resistor", "capacitor", "inductor"}

# Header names we accept (case-insensitive, stripped) for each field.
_HEADER_ALIASES: dict[str, set[str]] = {
    "references": {
        "reference", "references", "designator", "designators", "ref", "refs",
    },
    "value": {"value"},
    "footprint": {"footprint", "footprints"},
    "quantity": {"qty", "quantity", "count"},
    "mpn": {
        "mpn", "manufacturer part number", "mfr part #", "mfr part number",
        "part number", "partnumber",
    },
    "manufacturer": {"manufacturer", "mfr", "manufacturer name"},
}

_MAX_SUBSTITUTES = 5
_MAX_LINES = 10_000  # a real board BOM is well under this; guards a huge upload


@dataclass
class ParsedLine:
    references: str
    reference_prefix: str | None
    category: str | None
    value: str | None
    footprint: str | None
    mpn: str | None
    manufacturer: str | None
    quantity: int


# --- parsing ---------------------------------------------------------------


def _map_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map our canonical field names to the file's actual header names."""
    mapping: dict[str, str] = {}
    for header in fieldnames:
        key = (header or "").strip().lower()
        for canonical, aliases in _HEADER_ALIASES.items():
            if key in aliases and canonical not in mapping:
                mapping[canonical] = header
    return mapping


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _prefix_of(references: str) -> str | None:
    first = references.split(",")[0].strip()
    match = re.match(r"[A-Za-z]+", first)
    return match.group(0).upper() if match else None


def _quantity(raw_qty: str | None, references: str) -> int:
    text = (raw_qty or "").strip()
    if text:
        try:
            # OverflowError too: int(float("1e400"))/int(float("inf")) raise it.
            return max(int(float(text)), 0)
        except (ValueError, OverflowError):
            pass
    # No usable Qty column — count the designators.
    return sum(1 for ref in references.split(",") if ref.strip())


def clean_value(value: str | None) -> float | None:
    """Parse a BOM value's magnitude, or None if it isn't a scalar.

    Takes the token before the first space or ``/`` (dropping tolerance/voltage
    suffixes like ``"1k 1%"`` / ``"10uF/50V"``) and reads it as an engineering
    value. Part numbers (``"AO3400A"``, ``"BLM15PD121SN1D"``) return None.
    """
    text = (value or "").strip()
    if not text:
        return None
    token = re.split(r"[\s/]", text, maxsplit=1)[0]
    try:
        return parse_engineering(token)
    except UnitParseError:
        return None


def parse_bom(data: bytes, *, filename: str) -> list[ParsedLine]:
    """Parse a KiCad BOM CSV into lines, or raise :class:`ValidationError`."""
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise ValidationError("the BOM file is empty")
    # Sniff the delimiter from the header line only — data cells (e.g. URLs) can
    # confuse the heuristic; the header is simple. Default to comma.
    header_line = next(
        (line for line in text.splitlines() if line.strip()), ""
    )
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            header_line, delimiters=",;\t"
        )
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValidationError("the BOM file has no header row")
    columns = _map_columns(list(reader.fieldnames))
    if "references" not in columns or "value" not in columns:
        raise ValidationError(
            "the BOM needs at least a Reference and a Value column"
        )

    lines: list[ParsedLine] = []
    for row in reader:
        references = _clean(row.get(columns["references"]))
        if references is None:
            continue
        prefix = _prefix_of(references)
        lines.append(
            ParsedLine(
                references=references,
                reference_prefix=prefix,
                category=_PREFIX_CATEGORY.get(prefix) if prefix else None,
                value=_clean(row.get(columns["value"])),
                footprint=_clean(row.get(columns.get("footprint", ""))),
                mpn=_clean(row.get(columns.get("mpn", ""))),
                manufacturer=_clean(row.get(columns.get("manufacturer", ""))),
                quantity=_quantity(row.get(columns.get("quantity", "")), references),
            )
        )
        if len(lines) > _MAX_LINES:
            raise ValidationError(f"the BOM has more than {_MAX_LINES} lines")
    if not lines:
        raise ValidationError("the BOM has no usable lines")
    return lines


# --- persistence -----------------------------------------------------------


def create_bom(
    session: Session, *, name: str, filename: str, data: bytes, user_id: int
) -> Bom:
    """Parse and store a BOM (+ the original CSV as an attachment)."""
    lines = parse_bom(data, filename=filename)  # validate before writing anything
    bom = Bom(name=name or filename, source_filename=filename, created_by=user_id)
    session.add(bom)
    session.flush()  # assign bom.id for the lines
    for parsed in lines:
        session.add(BomLine(bom_id=bom.id, **vars(parsed)))
    session.commit()
    session.refresh(bom)
    bom_id = cast(int, bom.id)
    try:
        attachment_service.create_attachment(
            session,
            entity_type="bom",
            entity_id=bom_id,
            kind=AttachmentKind.OTHER,
            filename=filename,
            data=data,
        )
    except Exception:
        # The attachment validates independently (size/filename); don't leave a
        # committed bom with no stored CSV behind.
        delete_bom(session, bom_id)
        raise
    return bom


def reimport_bom(session: Session, bom_id: int) -> Bom:
    """Re-parse the BOM's stored CSV, replacing its lines.

    Lines freeze what the parser made of the CSV (category, prefix, value, MPN,
    quantity); stock matching is live, so only these frozen fields go stale — when
    the parser or its rules improve, or the header mapping was read differently.
    Re-runs exactly the import-time computation (``parse_bom``) over the original
    file, which is why no upload is needed.

    Parses BEFORE deleting anything, so an unreadable CSV leaves the existing lines
    untouched rather than emptying the BOM.
    """
    bom = get_bom(session, bom_id)
    attachments = attachment_service.list_attachments(
        session, entity_type="bom", entity_id=bom_id
    )
    # Pick the BOM's OWN file by name, not whatever is attached first: the
    # attachments API accepts (and can delete) bom attachments, and rebuilding the
    # lines from some other file would replace them from a source that was never
    # this BOM — a successful parse can't tell that apart from the real thing.
    stored = next(
        (a for a in attachments if a.filename == bom.source_filename), None
    )
    if stored is None:
        raise ValidationError("the original CSV is no longer stored for this BOM")
    path = attachment_service.stored_file_path(stored)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationError("the stored CSV could not be read") from exc

    parsed = parse_bom(data, filename=bom.source_filename or "bom.csv")
    for line in get_bom_lines(session, bom_id):
        session.delete(line)
    for line_data in parsed:
        session.add(BomLine(bom_id=bom_id, **vars(line_data)))
    # Assignments key on the designator group, so one survives a re-parse that
    # leaves its line alone. Drop those whose group is gone from the file: the
    # alternative is state that still exists but has no row to show it on, and so
    # no way to remove it — and that would silently come back if the designators
    # ever returned.
    kept = {line_data.references for line_data in parsed}
    for assignment in list_assignments(session, bom_id):
        if assignment.references not in kept:
            session.delete(assignment)
    session.commit()
    session.refresh(bom)
    return bom


def list_boms(session: Session) -> list[Bom]:
    """Return saved BOMs, newest first."""
    return list(session.exec(select(Bom).order_by(col(Bom.id).desc())).all())


def get_bom(session: Session, bom_id: int) -> Bom:
    return require_entity(session, Bom, bom_id, "bom")


def get_bom_lines(session: Session, bom_id: int) -> list[BomLine]:
    return list(
        session.exec(
            select(BomLine).where(BomLine.bom_id == bom_id).order_by(col(BomLine.id))
        ).all()
    )


def delete_bom(session: Session, bom_id: int) -> None:
    """Delete a BOM, its lines, its assignments and its stored CSV attachment."""
    bom = get_bom(session, bom_id)
    attachment_service.delete_attachments_for(
        session, entity_type="bom", entity_id=bom_id
    )
    link_service.delete_links_for(session, entity_type="bom", entity_id=bom_id)
    for assignment in list_assignments(session, bom_id):
        session.delete(assignment)
    for line in get_bom_lines(session, bom_id):
        session.delete(line)
    session.delete(bom)
    session.commit()


# --- assigning a component to a line ---------------------------------------


def list_assignments(session: Session, bom_id: int) -> list[BomLineAssignment]:
    """Every component assignment made on this BOM."""
    return list(
        session.exec(
            select(BomLineAssignment)
            .where(BomLineAssignment.bom_id == bom_id)
            .order_by(col(BomLineAssignment.id))
        ).all()
    )


def _require_line(session: Session, bom_id: int, line_id: int) -> BomLine:
    """The line, confirmed to belong to this BOM (so ids can't be crossed)."""
    line = require_entity(session, BomLine, line_id, "bom line")
    if line.bom_id != bom_id:
        raise NotFoundError(f"bom line {line_id} not found")
    return line


def _assignment_for(
    session: Session, bom_id: int, references: str
) -> BomLineAssignment | None:
    return session.exec(
        select(BomLineAssignment)
        .where(BomLineAssignment.bom_id == bom_id)
        .where(BomLineAssignment.references == references)
    ).first()


def assign_component(
    session: Session, bom_id: int, line_id: int, *, component_id: int, user_id: int
) -> BomLineAssignment:
    """Record that this line is built from ``component_id``.

    Deliberately unconditional: a line that already matches its MPN can still be
    pointed at a different part, because "what we actually build this from" is a
    decision the BOM's own text can't always express. One assignment per line —
    assigning again replaces the previous choice.
    """
    line = _require_line(session, bom_id, line_id)
    component = require_entity(session, Component, component_id, "component")
    if component.deleted_at is not None:
        raise ValidationError("that component is no longer in use")

    assignment = _assignment_for(session, bom_id, line.references)
    if assignment is None:
        assignment = BomLineAssignment(
            bom_id=bom_id,
            references=line.references,
            component_id=component_id,
            created_by=user_id,
        )
    else:
        assignment.component_id = component_id
        assignment.created_by = user_id
    session.add(assignment)
    session.commit()
    session.refresh(assignment)
    return assignment


def unassign_component(session: Session, bom_id: int, line_id: int) -> None:
    """Drop this line's assignment, returning it to plain MPN matching."""
    line = _require_line(session, bom_id, line_id)
    assignment = _assignment_for(session, bom_id, line.references)
    if assignment is None:
        raise NotFoundError(f"bom line {line_id} has no assigned component")
    session.delete(assignment)
    session.commit()


# --- availability report (live, read-only) ---------------------------------


def _value_parameter(
    session: Session, ctype: ComponentType
) -> ParameterDefinition | None:
    """The type's primary numeric parameter (lowest sort_order NUMBER def)."""
    number_defs = [
        d
        for d in cs.get_effective_parameter_definitions(session, cast(int, ctype.id))
        if d.data_type is ParameterDataType.NUMBER
    ]
    if not number_defs:
        return None
    return min(number_defs, key=lambda d: (d.sort_order, d.id or 0))


def _value_defs_by_category(
    session: Session, categories: set[str]
) -> dict[str, ParameterDefinition | None]:
    """Resolve each value-category to its type's primary numeric param, once.

    Avoids an N+1: computed a single time per report instead of per BOM line.
    """
    types_by_name = {
        t.name.lower(): t for t in session.exec(select(ComponentType)).all()
    }
    resolved: dict[str, ParameterDefinition | None] = {}
    for category in categories:
        ctype = types_by_name.get(category)
        resolved[category] = _value_parameter(session, ctype) if ctype else None
    return resolved


def _find_substitutes(
    line: BomLine,
    stock: dict[int, int],
    exclude: set[int],
    value_def: ParameterDefinition | None,
    session: Session,
) -> list[dict[str, object]]:
    """In-stock parts of the line's category with an equal/near value.

    Suggestions are per line and show each candidate's full on-hand stock; they
    are not allocated across lines, so two lines suggesting the same part both
    show the same quantity.
    """
    if value_def is None:
        return []
    target = clean_value(line.value)
    if target is None:
        return []

    tolerance = config.SUBSTITUTE_TOLERANCE_PCT / 100.0
    low = min(target * (1 - tolerance), target * (1 + tolerance))
    high = max(target * (1 - tolerance), target * (1 + tolerance))
    # Keyed on the value definition id, so inherited types (e.g. shunt under
    # resistor, which stores resistance under the same def) are included too.
    rows = session.exec(
        select(Component, ComponentParameter.value_num)
        .join(
            ComponentParameter,
            col(ComponentParameter.component_id) == col(Component.id),
        )
        .where(ComponentParameter.parameter_definition_id == value_def.id)
        .where(col(ComponentParameter.value_num) >= low)
        .where(col(ComponentParameter.value_num) <= high)
        .where(col(Component.deleted_at).is_(None))
    ).all()

    suggestions: list[dict[str, object]] = []
    for component, value_num in rows:
        component_id = cast(int, component.id)
        if component_id in exclude or value_num is None:
            continue
        on_hand = stock.get(component_id, 0)
        if on_hand <= 0:
            continue
        suggestions.append(
            {
                "component_id": component_id,
                "mpn": component.mpn,
                "package": component.package,
                "value": format_engineering(value_num, value_def.unit or ""),
                "stock": on_hand,
                "exact": value_num == target,
                "_delta": abs(value_num - target),
            }
        )
    suggestions.sort(key=lambda s: (not s["exact"], s["_delta"]))
    for suggestion in suggestions:
        del suggestion["_delta"]
    return suggestions[:_MAX_SUBSTITUTES]


def _components_by_id(session: Session, ids: set[int]) -> list[Component]:
    """Fetch several components at once, soft-deleted ones included.

    Assignments must still surface a component retired since it was chosen, so this
    deliberately does NOT filter on ``deleted_at`` — the caller decides what a
    retired one means.
    """
    if not ids:
        return []
    return list(
        session.exec(select(Component).where(col(Component.id).in_(ids))).all()
    )


def _short_footprint(value: str | None) -> str | None:
    """KiCad stores a footprint as ``Library:Name``; show just the ``Name`` part."""
    if value is None:
        return None
    return value.split(":", 1)[1] if ":" in value else value


def build_bom_report(
    session: Session, bom_id: int, *, boards: int = 1
) -> dict[str, object]:
    """Live availability report for a BOM against current stock (§21).

    ``boards`` is how many copies of the board are being built: it scales what each
    line *needs* (``total_quantity``), and hence its status, but not what stock can
    *cover* — ``boards_possible`` (per line) and ``summary.buildable`` (its minimum)
    stay measured in whole boards, so they answer "how many boards will these parts
    make?" whatever number was asked for.

    ``summary.buildable`` counts whole boards from parts that are actually resolved —
    an exact MPN match, or a component someone assigned — so a line with no MPN
    (common while designing) and no assignment, or one missing from inventory, caps
    it at 0. Substitutes are surfaced per line but not counted toward buildability.
    """
    boards = max(1, boards)
    bom = get_bom(session, bom_id)
    lines = get_bom_lines(session, bom_id)
    stock = ss.total_quantities_by_component(session)
    assignments = list_assignments(session, bom_id)
    assigned_by_refs = {a.references: a for a in assignments}
    # Resolve the assigned components in one query, like the stock totals and the
    # value definitions below — not one `session.get` per line.
    assigned_components = {
        cast(int, c.id): c
        for c in _components_by_id(session, {a.component_id for a in assignments})
    }
    # Resolve each substitutable category's value parameter once (not per line).
    value_defs = _value_defs_by_category(
        session, {ln.category for ln in lines if ln.category in _VALUE_CATEGORIES}
    )

    counts = {"ok": 0, "short": 0, "out": 0, "missing": 0, "no_mpn": 0}
    buildable: int | None = None
    report_lines: list[dict[str, object]] = []

    for line in lines:
        # An assignment stands in for the MPN lookup: someone has said what this
        # line is actually built from, so "no MPN" and "not in inventory" no longer
        # describe it — only how much of that part is on the shelf.
        assignment = assigned_by_refs.get(line.references)
        assigned_component = (
            assigned_components.get(assignment.component_id) if assignment else None
        )
        # A component soft-deleted after being assigned keeps the assignment visible
        # (so it can be seen and changed) but contributes no stock.
        assigned_live = (
            assigned_component is not None and assigned_component.deleted_at is None
        )

        if assigned_live:
            matched = [cast(Component, assigned_component)]
        elif assignment is not None:
            matched = []
        else:
            matched = cs.find_components_by_mpn(session, line.mpn) if line.mpn else []
        matched_stock = sum(stock.get(cast(int, c.id), 0) for c in matched)
        # What the whole run needs, not just one board.
        required = line.quantity * boards

        if assignment is not None and not assigned_live:
            status = "missing"  # assigned to a part that is no longer in use
        elif assignment is None and not line.mpn:
            status = "no_mpn"
        elif not matched:
            status = "missing"
        elif matched_stock >= required:
            status = "ok"
        elif matched_stock > 0:
            status = "short"
        else:
            status = "out"
        counts[status] += 1

        # Whole boards this line's stock covers — measured per BOARD, so it stays
        # the honest "these parts are enough for N boards" answer even when the run
        # asked for more. "Buildable boards" is then the limiting line across the
        # WHOLE BOM: a line with no matched stock (missing / no MPN) caps it at 0,
        # so the number reflects true buildability, not just the resolved lines.
        per_line = matched_stock // line.quantity if line.quantity else 0
        buildable = per_line if buildable is None else min(buildable, per_line)

        # Substitutes answer "what else could go here?" — a question already
        # answered once a component has been assigned.
        substitutes: list[dict[str, object]] = []
        if status != "ok" and assignment is None:
            substitutes = _find_substitutes(
                line,
                stock,
                {c.id for c in matched if c.id is not None},
                value_defs.get(line.category or ""),
                session,
            )

        report_lines.append(
            {
                "id": line.id,  # the UI addresses a line by id to assign to it
                "references": line.references,
                "reference_prefix": line.reference_prefix,
                "category": line.category,
                "value": line.value,
                "footprint": _short_footprint(line.footprint),
                "mpn": line.mpn,
                "manufacturer": line.manufacturer,
                "quantity": line.quantity,  # per board
                "total_quantity": required,  # for the whole run
                "boards_possible": per_line,  # what the stock on hand covers
                "status": status,
                "stock": matched_stock,
                "matched": [
                    {
                        "component_id": c.id,
                        "mpn": c.mpn,
                        "package": c.package,
                        "stock": stock.get(cast(int, c.id), 0),
                    }
                    for c in matched
                ],
                "assigned": (
                    {
                        "component_id": assignment.component_id,
                        "mpn": (
                            assigned_component.mpn if assigned_component else None
                        ),
                        "package": (
                            assigned_component.package if assigned_component else None
                        ),
                        "deleted": not assigned_live,
                    }
                    if assignment is not None
                    else None
                ),
                "substitutes": substitutes,
            }
        )

    return {
        "bom": {
            "id": bom.id,
            "name": bom.name,
            "created_at": bom.created_at.isoformat(),
            "line_count": len(lines),
        },
        "summary": {
            "lines": len(lines),
            **counts,
            "buildable": buildable,
            "boards": boards,  # echoed so the UI can say "N of M requested"
        },
        "lines": report_lines,
    }

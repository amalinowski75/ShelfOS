"""Storage-location hierarchy business logic (spec §7).

Locations form a tree (room → rack → shelf → …). This service manages creation
and traversal while preventing cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, cast

from sqlmodel import Session, col, select

from app.models.enums import LocationType
from app.models.invoice import InvoiceLine
from app.models.location import ComponentLocation, Location
from app.services._common import require_entity
from app.services.errors import ValidationError

# A physical storage hierarchy is never remotely this deep; the cap keeps the
# recursive tree render (and any client walk) from a pathological chain.
_MAX_DEPTH = 32

# Ceiling on locations created by one bulk-generate call. Keeps a typo'd count
# from flooding the table — and the Locations page renders the whole tree up
# front, so an enormous forest would slow every visit, not just this request.
_MAX_BULK_NODES = 500


class _Unset:
    """Sentinel type distinguishing "field omitted" from an explicit ``None``."""


_UNSET: Final = _Unset()


@dataclass
class LocationNode:
    """A location plus its full path and nested children (for tree rendering)."""

    location: Location
    path: str
    children: list[LocationNode] = field(default_factory=list)


def location_tree(session: Session) -> list[LocationNode]:
    """Return the location forest as nested nodes, each with its full path.

    Built from a single ``list_all`` query; roots first, children sorted by name.
    ``build`` descends only from the None-rooted forest, so a cyclic component
    (every node non-null parent) is never reached — no infinite recursion —
    while ``path_of`` walks up with a visited-set guard (mirroring get_path).
    """
    locations = list_all(session)
    by_id = {loc.id: loc for loc in locations}
    children: dict[int | None, list[Location]] = {}
    for loc in locations:
        children.setdefault(loc.parent_id, []).append(loc)
    for group in children.values():
        group.sort(key=lambda loc: loc.name.lower())

    def path_of(loc: Location) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        current: Location | None = loc
        while current is not None and current.id not in seen:
            seen.add(cast(int, current.id))
            parts.append(current.name)
            current = by_id.get(current.parent_id)
        return " / ".join(reversed(parts))

    def build(parent_id: int | None) -> list[LocationNode]:
        return [
            LocationNode(
                location=loc,
                path=path_of(loc),
                children=build(cast(int, loc.id)),
            )
            for loc in children.get(parent_id, [])
        ]

    return build(None)


def flatten_tree(nodes: list[LocationNode]) -> list[LocationNode]:
    """Pre-order flattening of a location forest (parents before children)."""
    flat: list[LocationNode] = []
    for node in nodes:
        flat.append(node)
        flat.extend(flatten_tree(node.children))
    return flat


def list_all(session: Session) -> list[Location]:
    """Return every location ordered by name (for selection dropdowns)."""
    return list(session.exec(select(Location).order_by(col(Location.name))).all())


def create_location(
    session: Session,
    *,
    type: LocationType,
    name: str,
    parent_id: int | None = None,
) -> Location:
    """Create a location, optionally nested under a parent location."""
    if not name.strip():
        raise ValidationError("location name must not be empty")
    if parent_id is not None:
        require_entity(session, Location, parent_id, "location")
        if len(get_path(session, parent_id)) >= _MAX_DEPTH:
            raise ValidationError(
                f"location hierarchy may not be deeper than {_MAX_DEPTH} levels"
            )
    location = Location(type=type, name=name, parent_id=parent_id)
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


def get_path(session: Session, location_id: int) -> list[Location]:
    """Return the location chain from root to the given location (inclusive)."""
    chain: list[Location] = []
    seen: set[int] = set()
    current: int | None = location_id
    while current is not None:
        if current in seen:
            raise ValidationError(f"cycle detected in location hierarchy at {current}")
        seen.add(current)
        location = require_entity(session, Location, current, "location")
        chain.append(location)
        current = location.parent_id
    chain.reverse()
    return chain


def get_children(session: Session, parent_id: int | None) -> list[Location]:
    """Return the direct children of a location (or roots when ``None``)."""
    return list(
        session.exec(
            select(Location)
            .where(Location.parent_id == parent_id)
            .order_by(Location.name)
        ).all()
    )


def format_path(session: Session, location_id: int) -> str:
    """Return a human-readable path such as ``"Lab / Rack A / Shelf 1"``."""
    return " / ".join(loc.name for loc in get_path(session, location_id))


@dataclass
class BulkLevel:
    """One level of a generated hierarchy: ``count`` locations of ``type`` per
    parent, named by ``name_pattern`` (``{n}`` is the 1-based, zero-padded
    number; ``None`` falls back to ``"<Type> {n}"``)."""

    type: LocationType
    count: int
    name_pattern: str | None = None


@dataclass
class BulkGenerateResult:
    """What a bulk generation did (or, for a dry run, would do)."""

    total: int
    created_ids: list[int]
    sample_paths: list[str]


def _level_names(level: BulkLevel) -> list[str]:
    """The sibling names one level generates under a single parent.

    Numbers are zero-padded to the count's width because sibling sort is plain
    ``name.lower()`` — without padding "Shelf 10" would sort before "Shelf 2".
    """
    pattern = (level.name_pattern or f"{level.type.value.capitalize()} {{n}}").strip()
    if not pattern:
        raise ValidationError("level name pattern must not be empty")
    if level.count < 1:
        raise ValidationError("level count must be at least 1")
    if level.count > 1 and "{n}" not in pattern:
        raise ValidationError(
            f"name pattern {pattern!r} needs an {{n}} placeholder when count > 1"
        )
    width = len(str(level.count))
    return [
        pattern.replace("{n}", str(n).zfill(width)) for n in range(1, level.count + 1)
    ]


def generate_locations(
    session: Session,
    *,
    parent_id: int | None,
    levels: list[BulkLevel],
    dry_run: bool = False,
) -> BulkGenerateResult:
    """Generate a whole hierarchy (e.g. rack → shelves → drawers) in one call.

    Each level multiplies: every location of level N gets ``count`` children of
    level N+1. A dry run reports the totals and sample paths without touching
    the database; a real run is one transaction — either the whole hierarchy
    lands or none of it. First-level names clashing (case-insensitively) with
    existing children of the parent are rejected rather than suffixed, so a
    re-run after a partial plan never silently doubles a cabinet.
    """
    if not levels:
        raise ValidationError("at least one level is required")

    base_path = ""
    base_depth = 0
    if parent_id is not None:
        path = get_path(session, parent_id)
        base_depth = len(path)
        base_path = " / ".join(loc.name for loc in path)
    if base_depth + len(levels) > _MAX_DEPTH:
        raise ValidationError(
            f"location hierarchy may not be deeper than {_MAX_DEPTH} levels"
        )

    names_per_level = [_level_names(level) for level in levels]
    total = 0
    branch = 1
    for names in names_per_level:
        branch *= len(names)
        total += branch
    if total > _MAX_BULK_NODES:
        raise ValidationError(
            f"this would create {total} locations; "
            f"the limit per request is {_MAX_BULK_NODES}"
        )

    existing = {loc.name.lower() for loc in get_children(session, parent_id)}
    conflicts = [name for name in names_per_level[0] if name.lower() in existing]
    if conflicts:
        raise ValidationError(
            "these names already exist under the chosen parent: " + ", ".join(conflicts)
        )

    created_ids: list[int] = []
    parents: list[tuple[int | None, str]] = [(parent_id, base_path)]
    for level, names in zip(levels, names_per_level, strict=True):
        children: list[tuple[int | None, str]] = []
        for pid, parent_path in parents:
            for name in names:
                child_path = f"{parent_path} / {name}" if parent_path else name
                if dry_run:
                    children.append((None, child_path))
                    continue
                location = Location(type=level.type, name=name, parent_id=pid)
                session.add(location)
                # flush (not commit) hands out the id the next level nests
                # under while keeping the whole run one transaction.
                session.flush()
                created_ids.append(cast(int, location.id))
                children.append((location.id, child_path))
        parents = children
    if not dry_run:
        session.commit()

    # The deepest level's first few paths show the full shape at a glance.
    sample_paths = [path for _, path in parents[:5]]
    return BulkGenerateResult(
        total=total, created_ids=created_ids, sample_paths=sample_paths
    )


def _subtree_height(session: Session, location_id: int) -> int:
    """Number of levels in the subtree rooted at ``location_id`` (a leaf is 1).

    Walks the whole forest from one query; a visited-set guards the recursion the
    same way ``get_path`` guards its walk, so a cyclic row can't hang it.
    """
    children: dict[int | None, list[int]] = {}
    for loc in list_all(session):
        children.setdefault(loc.parent_id, []).append(cast(int, loc.id))

    def height(node_id: int, seen: set[int]) -> int:
        if node_id in seen:
            return 0
        seen.add(node_id)
        kids = children.get(node_id, [])
        return 1 + max((height(kid, seen) for kid in kids), default=0)

    return height(location_id, set())


def update_location(
    session: Session,
    location_id: int,
    *,
    name: str | None | _Unset = _UNSET,
    parent_id: int | None | _Unset = _UNSET,
    type: LocationType | None | _Unset = _UNSET,
) -> Location:
    """Edit a location's name, type and/or parent; omitted fields stay unchanged.

    ``parent_id=None`` moves the location to the top level. A move is rejected
    when the new parent lies inside the moved subtree (a cycle) or when the
    subtree's deepest descendant would end up past the depth cap.
    """
    location = require_entity(session, Location, location_id, "location")
    if not isinstance(name, _Unset):
        if name is None or not name.strip():
            raise ValidationError("location name must not be empty")
        location.name = name
    if not isinstance(type, _Unset):
        if type is None:
            raise ValidationError("location type must not be null")
        location.type = type
    if not isinstance(parent_id, _Unset) and parent_id != location.parent_id:
        if parent_id is not None:
            parent_path = get_path(session, parent_id)
            if any(loc.id == location_id for loc in parent_path):
                raise ValidationError(
                    "cannot move a location under itself or one of its descendants"
                )
            depth = len(parent_path) + _subtree_height(session, location_id)
            if depth > _MAX_DEPTH:
                raise ValidationError(
                    f"location hierarchy may not be deeper than {_MAX_DEPTH} levels"
                )
        location.parent_id = parent_id
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


def _subtree_ids(session: Session, location_id: int) -> list[int]:
    """Every id in the subtree rooted at ``location_id``, parents before
    children. One ``list_all`` query; a visited set guards a cyclic row."""
    children: dict[int | None, list[int]] = {}
    for loc in list_all(session):
        children.setdefault(loc.parent_id, []).append(cast(int, loc.id))
    ids: list[int] = []
    stack = [location_id]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        ids.append(current)
        stack.extend(children.get(current, []))
    return ids


def delete_location(
    session: Session, location_id: int, *, recursive: bool = False
) -> int:
    """Delete a location — with ``recursive``, its whole branch. Returns how
    many locations went.

    Without ``recursive`` a location that still has children is refused, so a
    plain delete can never take more than the one row the user pointed at.
    With it, the subtree goes in one transaction — but stock still blocks: if
    ANY location in the branch holds a non-zero quantity, nothing is deleted
    and the error names where the stock sits.

    Zero-quantity ``ComponentLocation`` rows are cache, not history, and are
    cleaned up; invoice lines pointing anywhere in the branch get
    ``location_id`` cleared so the invoice page keeps rendering (the line
    survives, its destination reads as unset). ``StockMovement`` rows keep
    their ``location_id`` — the ledger (§17) is immutable and the UI never
    resolves a movement's location name.

    Note: SQLite may hand a new row the highest deleted rowid back, so a label
    printed for a deleted location could scan to a later one — reprint labels
    after reorganising.
    """
    require_entity(session, Location, location_id, "location")
    if not recursive and get_children(session, location_id):
        raise ValidationError(
            "location still has child locations; delete or move them first"
        )
    ids = _subtree_ids(session, location_id) if recursive else [location_id]
    slots = list(
        session.exec(
            select(ComponentLocation).where(col(ComponentLocation.location_id).in_(ids))
        ).all()
    )
    stocked = next((slot for slot in slots if slot.quantity != 0), None)
    if stocked is not None:
        raise ValidationError(
            f"'{format_path(session, stocked.location_id)}' still holds stock; "
            "move or remove it first"
        )
    for slot in slots:
        session.delete(slot)
    lines = session.exec(
        select(InvoiceLine).where(col(InvoiceLine.location_id).in_(ids))
    ).all()
    for line in lines:
        line.location_id = None
        session.add(line)
    # Children before parents, so a mid-transaction failure can never leave an
    # orphan pointing at a deleted parent.
    for current in reversed(ids):
        location = session.get(Location, current)
        if location is not None:
            session.delete(location)
    session.commit()
    return len(ids)

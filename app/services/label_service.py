"""Printable location labels: a small QR plus the human-readable path (spec §7).

The QR encodes ``SL<id>`` — deliberately terse, and all characters from the QR
alphanumeric set (digits + uppercase), which packs ~1.7× denser than byte mode.
That keeps every realistic id in QR version 1 (21×21 modules, the smallest —
version 1 at ECC M holds "SL" + 18 digits), so the code prints legibly even on
9 mm of a 12 mm tape. A lowercase or ``:``-ed prefix would force byte mode and
spill three-digit ids into version 2 already.

The prefix reserves room for a future scan dispatcher (``^SL(\\d+)$``) to tell
a location label apart from a supplier barcode (cf.
``app/services/shops/scan.py``), enabling a scan-the-bag / scan-the-drawer
stock flow later.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import cast

import segno
from sqlmodel import Session

from app.models.location import Location
from app.services import location_service as ls
from app.services._common import require_entity

_QR_PREFIX = "SL"


def location_qr_payload(location_id: int) -> str:
    """The string a location label's QR encodes (uppercase — see module doc)."""
    return f"{_QR_PREFIX}{location_id}"


def qr_svg(payload: str) -> str:
    """Render a payload as an inline SVG fragment.

    No XML declaration (it is embedded in HTML) and no fixed size — the viewBox
    lets CSS scale it to whatever label height the print page asks for.
    ``micro=False``: segno would happily emit a Micro QR for a payload this
    short, and phone cameras are unreliable with those; a full QR reads
    anywhere.
    """
    buffer = io.BytesIO()
    segno.make(payload, error="m", micro=False).save(
        buffer, kind="svg", xmldecl=False, omitsize=True, svgclass=None, lineclass=None
    )
    return buffer.getvalue().decode()


@dataclass
class LabelData:
    """Everything one printed label shows."""

    id: int
    name: str
    path: str
    qr_svg: str


def build_labels(
    session: Session,
    *,
    ids: list[int] | None = None,
    root: int | None = None,
) -> list[LabelData]:
    """Labels for explicit ``ids``, for a subtree (``root`` and everything under
    it), or — with neither — for every location, all in tree pre-order."""
    forest = ls.location_tree(session)
    nodes = ls.flatten_tree(forest)
    if root is not None:
        require_entity(session, Location, root, "location")
        subtree = next(node for node in nodes if node.location.id == root)
        nodes = ls.flatten_tree([subtree])
    elif ids is not None:
        by_id = {node.location.id: node for node in nodes}
        for location_id in ids:
            if location_id not in by_id:
                require_entity(session, Location, location_id, "location")
        nodes = [by_id[location_id] for location_id in ids]
    return [
        LabelData(
            id=cast(int, node.location.id),
            name=node.location.name,
            path=node.path,
            qr_svg=qr_svg(location_qr_payload(cast(int, node.location.id))),
        )
        for node in nodes
    ]

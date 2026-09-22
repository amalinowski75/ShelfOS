"""Printable labels: a small QR plus what the thing is (spec §7).

Two kinds of thing get a label here — a location and a component — and both are
described by the same four facts: an id, a headline, a line of detail, and the
string the QR encodes. The renderers (``labels.html`` for the browser,
``label_printer`` for the tape) take that and nothing else, so neither of them
knows what a location or a component is.

The QR encodes ``SL<id>`` for a location and ``SC<id>`` for a component —
deliberately terse, and all characters from the QR alphanumeric set (digits +
uppercase), which packs ~1.7× denser than byte mode. That keeps every realistic
id in QR version 1 (21×21 modules, the smallest — version 1 at ECC M holds a
two-letter prefix and 18 digits), so the code prints legibly even on 9 mm of a
12 mm tape. A lowercase or ``:``-ed prefix would force byte mode and spill
three-digit ids into version 2 already.

The prefix is what tells our own labels apart from a supplier barcode (cf.
``app/services/shops/scan.py``): ``^SL(\\d+)$`` is a shelf, and scanning one
files whatever is in hand there; ``^SC(\\d+)$`` is a part, and scanning one
names that part outright — no part number to look up, and none of the
ambiguity that comes with an MPN two companies both print.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Literal, cast

import segno
from sqlmodel import Session

from app.models.component import Component
from app.models.location import Location
from app.services import component_service as cs
from app.services import location_service as ls
from app.services._common import require_entity
from app.services.errors import ValidationError

_LOCATION_PREFIX = "SL"
_COMPONENT_PREFIX = "SC"

# How ``location_service`` spells a path; the label reuses it as the point a
# long path may be broken at, so a wrapped label breaks between two locations
# rather than in the middle of one's name.
_PATH_SEPARATOR = " / "

# A component's detail is prose, so it breaks at spaces like prose.
_PROSE_SEPARATOR = " "

# Past SQLite's 64-bit rowid range the driver raises OverflowError mid-query. An
# id that far out cannot name anything, so a scan carrying one is read as "not
# one of ours" and goes on to be tried as a supplier barcode.
_MAX_ROWID = 2**63 - 1

# The longest any one piece of a label's text may be. Every field a component
# label draws from — ``mpn``, ``manufacturer``, ``notes`` — is uncapped free
# text (none of them carries a ``max_length``), and the fitter measures the
# string it is given once per type size it tries; when it finally gives up
# shrinking, ``_ellipsise`` walks a line that has nothing to wrap at one
# character at a time, measuring each. So a paragraph pasted into any of the
# three is real work for one GET of a preview. Everything past this is the
# detail page's business, not the label's.
_MAX_TEXT_CHARS = 120

_COMPONENT_LABEL = re.compile(rf"^{_COMPONENT_PREFIX}(\d+)$", re.IGNORECASE)

#: Which end of the detail is given up when it will not fit at any size.
#: A path is read from the right — the drawer identifies the label and the room
#: is context you already have, standing in it — so it loses its head. Prose is
#: read from the left and loses its tail.
Trim = Literal["head", "tail"]


def location_qr_payload(location_id: int) -> str:
    """The string a location label's QR encodes (uppercase — see module doc)."""
    return f"{_LOCATION_PREFIX}{location_id}"


def component_qr_payload(component_id: int) -> str:
    """The string a component label's QR encodes (uppercase — see module doc)."""
    return f"{_COMPONENT_PREFIX}{component_id}"


def scanned_component_id(code: str) -> int | None:
    """The component one of our own labels names, or ``None`` for anything else.

    The one server-side reading of the ``SC<id>`` format, so a scan resolves to
    a part the same way wherever it is scanned. Case-insensitive: plenty of QR
    readers hand back what alphanumeric mode encoded, which is upper case, but
    a code typed into the field by hand need not be.
    """
    matched = _COMPONENT_LABEL.match(code.strip())
    if matched is None:
        return None
    component_id = int(matched.group(1))
    return component_id if 0 < component_id <= _MAX_ROWID else None


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
    """Everything one printed label shows.

    ``name`` is the headline, read across the room; ``detail`` is the small
    print under (or beside) it, and may wrap. A newline in ``detail`` is a break
    the label keeps — it is how a component puts its maker on a line of its own
    instead of letting the description run up against it.

    ``separator`` and ``trim`` are the two things a renderer cannot work out
    from the text: where the detail may be broken, and which end to sacrifice
    when it still will not fit. Both are stated outright rather than defaulted,
    because the wrong answer to either is not a crash — it is a label that comes
    off the tape looking fine and saying the wrong thing.

    ``qr_svg`` is for the browser page only, and is empty for labels that are
    built to go straight onto tape — the printer rasterises the payload itself.
    """

    id: int
    name: str
    detail: str
    qr_payload: str
    separator: str
    trim: Trim
    qr_svg: str = ""


def build_labels(
    session: Session,
    *,
    ids: list[int] | None = None,
    root: int | None = None,
) -> list[LabelData]:
    """Labels for explicit ``ids``, for a subtree (``root`` and everything under
    it), or — with neither — for every location, all in tree pre-order.

    ``ids`` are deduplicated (order preserved) and capped. The cap is the
    bulk-create one: what one request may create, one request may print.
    """
    if ids is not None:
        ids = _dedupe_and_cap(ids, "print a subtree with root=… instead")
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
            detail=node.path,
            qr_payload=location_qr_payload(cast(int, node.location.id)),
            separator=_PATH_SEPARATOR,
            trim="head",
            qr_svg=qr_svg(location_qr_payload(cast(int, node.location.id))),
        )
        for node in nodes
    ]


def build_component_labels(session: Session, ids: list[int]) -> list[LabelData]:
    """Labels for the named components, in the order they were asked for.

    The part number is the headline — it is what somebody holding the bag reads
    to know what is in it — with the maker and the description under it. The
    maker keeps its own line: a part number is only half an identity (two
    companies print the same number on different parts), so who made this one
    should not have to be picked out of a sentence.

    No SVG is rendered: these print to tape, and there is no browser page for
    them to be embedded in.
    """
    ids = _dedupe_and_cap(ids, "print them in batches")
    found = cs.components_by_id(session, set(ids))
    for component_id in ids:
        if component_id not in found:
            require_entity(session, Component, component_id, "component")
        # ``components_by_id`` deliberately includes soft-deleted parts, and a
        # retired one must not get tape: scanning that very label answers "this
        # label is out of date" (see the scan endpoint), so printing it would be
        # laying down a label the rest of the system has already disowned.
        if found[component_id].deleted_at is not None:
            raise ValidationError(
                f"{found[component_id].mpn or f'component #{component_id}'} has "
                "been deleted from the inventory; there is nothing to label"
            )
    return [
        LabelData(
            id=component_id,
            name=_clip(found[component_id].mpn) or f"Component #{component_id}",
            detail=_component_detail(found[component_id]),
            qr_payload=component_qr_payload(component_id),
            separator=_PROSE_SEPARATOR,
            trim="tail",
        )
        for component_id in ids
    ]


def _component_detail(component: Component) -> str:
    """Who makes the part, and what it is — one per line, blanks left out."""
    return "\n".join(
        part
        for part in (_clip(component.manufacturer), _clip(component.notes))
        if part
    )


def _clip(text: str | None) -> str:
    """One field of free text, made safe to measure and to draw.

    Collapsed to single spaces — the value may carry the line breaks it was
    pasted with, and those are not the label's breaks, which are its own — and
    cut to ``_MAX_TEXT_CHARS`` for the reason given there. The cut is marked,
    so a shortened description does not read as the whole of one.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= _MAX_TEXT_CHARS:
        return collapsed
    return collapsed[:_MAX_TEXT_CHARS].rstrip() + "…"


def _dedupe_and_cap(ids: list[int], advice: str) -> list[int]:
    """Ids with repeats dropped (order kept), refused if there are too many.

    The list arrives straight from a request, and each entry costs a QR render,
    so an uncapped repeat-the-id request would be free heavy work for any
    reader. ``advice`` says what to do instead, which differs by what is being
    labelled.
    """
    ids = list(dict.fromkeys(ids))
    if len(ids) > ls._MAX_BULK_NODES:
        raise ValidationError(
            f"at most {ls._MAX_BULK_NODES} labels per request; {advice}"
        )
    return ids

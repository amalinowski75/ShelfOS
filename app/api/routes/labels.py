"""Label endpoints: what a location's label looks like, and printing it (spec §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import LabelPrintRequest, LabelPrintResult
from app.services import label_printer as lp
from app.services import label_service as lbl

router = APIRouter(prefix="/api/labels", tags=["labels"])

# Python parses an int of any size, but past SQLite's 64-bit rowid range the
# driver raises OverflowError mid-query — an unmapped 500. Same guard, and the
# same reason, as the labels page in ``app/web/routes.py``.
_MAX_ROWID = 2**63 - 1


@router.get("/locations/{location_id}/preview.png")
def preview_location_label(
    location_id: int,
    tape: str | None = None,
    length: float | None = None,
    session: Session = Depends(get_session),
) -> Response:
    """The exact bitmap that would go to the label printer, as a PNG.

    Deliberately independent of whether a printer is configured: this is how the
    layout gets tuned — open it, look, change a setting, reload — and that has to
    work before any printer is plugged in. ``tape``/``length`` override the
    configured tape for one render, so a roll can be tried without a restart.
    """
    if not 0 < location_id <= _MAX_ROWID:
        raise HTTPException(
            status_code=422, detail="location id must be a positive 64-bit integer"
        )
    label = lbl.build_labels(session, ids=[location_id])[0]
    geometry = (
        lp.tape_geometry(tape=tape, length_mm=length)
        if tape is not None or length is not None
        else None
    )
    return Response(content=lp.render_png(label, geometry), media_type="image/png")


@router.post("/locations/print", response_model=LabelPrintResult)
def print_location_labels(
    payload: LabelPrintRequest, session: Session = Depends(get_session)
) -> LabelPrintResult:
    """Send location labels to the label printer (writers).

    The selection is ``label_service.build_labels`` — the same one the printable
    page uses — so a branch prints exactly what its preview showed. The reply
    says how many were *sent*: a one-way write to the printer cannot tell
    whether tape came out the other side.
    """
    labels = lbl.build_labels(session, ids=payload.ids, root=payload.root)
    return LabelPrintResult(sent=lp.print_labels(labels, copies=payload.copies))

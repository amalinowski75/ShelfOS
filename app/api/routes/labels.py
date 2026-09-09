"""Label endpoints: what a location's label looks like, and printing it (spec §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session

from app import config
from app.api.deps import get_session
from app.api.schemas import (
    LabelJobRead,
    LabelPrintRequest,
    LabelPrintResult,
    PrinterProbeRead,
    TapeRead,
    TapesRead,
)
from app.services import label_printer as lp
from app.services import label_service as lbl
from app.services import label_setup as setup

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
    # Preview what the printer would actually produce, which means asking it
    # what tape it holds, exactly as printing does; with no printer answering,
    # the configured tape stands. An override replaces only what it names — a
    # length alone must not quietly take the width back off the printer.
    if tape is None and length is None:
        geometry = lp.resolve_geometry()
    else:
        geometry = lp.tape_geometry(
            tape=tape if tape is not None else lp.resolve_geometry().tape,
            length_mm=length,
        )
    return Response(content=lp.render_png(label, geometry), media_type="image/png")


@router.post("/locations/print", response_model=LabelPrintResult)
def print_location_labels(
    payload: LabelPrintRequest, session: Session = Depends(get_session)
) -> LabelPrintResult:
    """Send location labels to the label printer (writers).

    The selection is ``label_service.build_labels`` — the same one the printable
    page uses — so a branch prints exactly what its preview showed. The reply
    says how many labels went and whether the printer confirmed printing them;
    it refuses up front when the printer reports a problem, or holds tape the
    configured one does not match.
    """
    labels = lbl.build_labels(session, ids=payload.ids, root=payload.root)
    outcome = lp.print_labels(
        labels,
        copies=payload.copies,
        tape=payload.tape,
        accept_loaded=payload.accept_loaded,
    )
    return LabelPrintResult(
        sent=outcome.sent,
        confirmed=outcome.confirmed,
        tape=outcome.tape,
        stopped=outcome.stopped,
    )


@router.get("/tapes", response_model=TapesRead)
def list_tapes() -> TapesRead:
    """The rolls that can be picked, and what the printer says it is holding.

    ``loaded`` is best-effort: an unplugged or silent printer simply leaves it
    null, and the picker then has nothing to pre-select but the configuration.
    """
    # Asked only while the printer is idle: three bytes on the wire during a job
    # would be spliced into the raster. A silent answer is what the dialog
    # already copes with, so the degraded reply costs nothing.
    status = lp.status_if_free()
    return TapesRead(
        tapes=[TapeRead(**vars(choice)) for choice in lp.tape_choices()],
        configured=config.LABEL_TAPE,
        loaded=lp.detect_tape(status) if status is not None else None,
    )


@router.post("/stop", response_model=LabelJobRead)
def stop_printing() -> LabelJobRead:
    """Ask a running print to stop after the label it is on (writers).

    The labels already handed to the printer are being printed and cannot be
    recalled — the buffer belongs to the machine. What this stops is everything
    not yet sent, which for a cabinet started by mistake is nearly all of it.
    """
    lp.request_stop()
    progress = lp.job_progress()
    return LabelJobRead(
        printing=progress is not None,
        done=progress[0] if progress else 0,
        total=progress[1] if progress else 0,
    )


@router.get("/job", response_model=LabelJobRead)
def running_job() -> LabelJobRead:
    """How far the running print has got, if there is one."""
    progress = lp.job_progress()
    return LabelJobRead(
        printing=progress is not None,
        done=progress[0] if progress else 0,
        total=progress[1] if progress else 0,
    )


@router.get("/setup/installer.sh")
def download_installer(
    ssh_user: str,
    ssh_host: str,
    ssh_port: int = setup.DEFAULT_SSH_PORT,
    device: str = setup.DEFAULT_DEVICE,
    bridge_port: int = setup.DEFAULT_BRIDGE_PORT,
    group: str = setup.ALLOWED_GROUPS[0],
) -> Response:
    """The setup script for the machine holding the printer, filled in (§7).

    A GET taking the answers in the query string, because the form that sends
    it is a plain ``<form method="get">``: a download built in JavaScript would
    be the only Blob in the whole interface, and this needs no session state.
    Every value is validated in the service, and a bad one comes back as a 422
    rather than being escaped into the script.
    """
    script = setup.render_installer(
        ssh_user=ssh_user,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        device=device,
        bridge_port=bridge_port,
        group=group,
    )
    return Response(
        content=script,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{setup.INSTALLER_FILENAME}"'
            ),
            # It names a server and an account. Not a secret, but there is no
            # reason for a proxy or the browser to keep a copy.
            "Cache-Control": "no-store",
        },
    )


@router.get("/setup/probe", response_model=PrinterProbeRead)
def probe_printer(device: str | None = None) -> PrinterProbeRead:
    """Ask the printer how it is, and say what came of asking (§7).

    200 in every reachable case, including "it did not answer". That is the
    answer the button asked for, not a failure of the request — unlike the
    print path, where a 503 is right because silence defeats what the caller
    wanted. A GET, like ``/tapes``, which has always asked the printer this way;
    that also keeps the button working for a read-only account, which is the
    point, since it is about the machine in front of the person rather than
    their permissions in ShelfOS.
    """
    target = setup.probe_target(device or config.LABEL_DEVICE)
    result = lp.probe_device(target)
    return PrinterProbeRead(
        answered=result.answered,
        busy=result.busy,
        detail=result.detail,
        tape=result.tape,
        width_mm=result.width_mm,
        errors=list(result.errors),
    )

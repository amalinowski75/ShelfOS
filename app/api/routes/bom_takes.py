"""Taking a BOM off the shelves (spec §21) — preview, run, read, undo.

Mounted under the protected routers: read-only accounts can read a snapshot but
not make or reverse one.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import BomTakeRead, BomTakeRequest, BomTakeReverse
from app.auth.deps import current_user_id
from app.models.bom_take import BomTake
from app.services import bom_take_service as svc
from app.services import location_service as ls

router = APIRouter(tags=["bom takes"])


def _answers(payload: BomTakeRequest) -> tuple[dict[int, int], dict[int, int]]:
    """Split the per-line answers into the two maps the service takes."""
    overrides = {
        line.line_id: line.quantity
        for line in payload.lines
        if line.quantity is not None
    }
    choices = {
        line.line_id: line.source_location_id
        for line in payload.lines
        if line.source_location_id is not None
    }
    return overrides, choices


def _plan_json(plan: svc.TakePlan) -> dict[str, object]:
    def source(entry: svc.PlannedSource) -> dict[str, object]:
        return {
            "location_id": entry.location_id,
            "path": entry.path,
            "available": entry.available,
            "quantity": entry.quantity,
            "inside": entry.inside,
        }

    return {
        "bom_id": plan.bom_id,
        "bom_name": plan.bom_name,
        "boards": plan.boards,
        "source_location_id": plan.source_location_id,
        "source_path": plan.source_path,
        "can_run": plan.can_run,
        "blocked_references": plan.blocked_references,
        "unanswered_references": plan.unanswered_references,
        "total_shortfall": plan.total_shortfall,
        "lines": [
            {
                "line_id": line.line_id,
                "references": line.references,
                "mpn": line.mpn,
                "component_id": line.component_id,
                "per_board": line.per_board,
                "requested": line.requested,
                "shortfall": line.shortfall,
                "needs_choice": line.needs_choice,
                "blocked": line.blocked,
                "sources": [source(s) for s in line.sources],
                "candidates": [source(c) for c in line.candidates],
            }
            for line in plan.lines
        ],
    }


@router.post("/api/boms/{bom_id}/take/preview")
def preview_take(
    bom_id: int,
    payload: BomTakeRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """What this take would do, without doing it (writers).

    POST rather than GET because the edited quantities and the chosen locations
    are a body, and this is the confirm button's dry run.
    """
    overrides, choices = _answers(payload)
    plan = svc.plan_take(
        session,
        bom_id,
        boards=payload.boards,
        source_location_id=payload.source_location_id,
        overrides=overrides,
        choices=choices,
    )
    return _plan_json(plan)


@router.post(
    "/api/boms/{bom_id}/takes",
    response_model=BomTakeRead,
    status_code=status.HTTP_201_CREATED,
)
def create_take(
    bom_id: int,
    payload: BomTakeRequest,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> BomTake:
    """Take the parts off the shelves and record the snapshot (writers)."""
    overrides, choices = _answers(payload)
    return svc.execute_take(
        session,
        bom_id,
        boards=payload.boards,
        source_location_id=payload.source_location_id,
        overrides=overrides,
        choices=choices,
        user_id=user_id,
    )


@router.get("/api/boms/{bom_id}/takes", response_model=list[BomTakeRead])
def list_takes(bom_id: int, session: Session = Depends(get_session)) -> list[BomTake]:
    """Past takes of this BOM, newest first."""
    return svc.list_takes(session, bom_id)


@router.get("/api/bom-takes/{take_id}")
def get_take(
    take_id: int, session: Session = Depends(get_session)
) -> dict[str, object]:
    """One snapshot: header, lines, and where each part came from."""
    return take_detail(session, take_id)


@router.post("/api/bom-takes/{take_id}/reverse", response_model=BomTakeRead)
def reverse_take(
    take_id: int,
    payload: BomTakeReverse,
    session: Session = Depends(get_session),
    user_id: int = Depends(current_user_id),
) -> BomTake:
    """Put everything back, saying why (writers)."""
    return svc.reverse_take(
        session, take_id, reason=payload.reason, user_id=user_id
    )


def take_detail(session: Session, take_id: int) -> dict[str, object]:
    """The snapshot as the page and the API both want it.

    Location paths are resolved defensively: `delete_location` refuses only on
    non-zero stock, so the gathering tree is deletable the moment a take empties
    it, and a snapshot must survive that as "—" rather than a 500.
    """
    take = svc.get_take(session, take_id)
    lines = svc.take_lines(session, take_id)
    allocations = svc.take_allocations(session, take_id)
    by_line: dict[int, list[dict[str, object]]] = {}
    for allocation in allocations:
        by_line.setdefault(allocation.take_line_id, []).append(
            {
                "location_id": allocation.location_id,
                "path": ls.path_or_dash(session, allocation.location_id),
                "quantity": allocation.quantity,
                "movement_id": allocation.movement_id,
                "reversal_movement_id": allocation.reversal_movement_id,
            }
        )
    return {
        "id": take.id,
        "bom_id": take.bom_id,
        "name": take.name,
        "boards": take.boards,
        "source_path": (
            ls.path_or_dash(session, take.source_location_id)
            if take.source_location_id is not None
            else "—"
        ),
        "created_at": take.created_at.isoformat(),
        "reversed_at": take.reversed_at.isoformat() if take.reversed_at else None,
        "reversal_reason": take.reversal_reason,
        "lines": [
            {
                "id": line.id,
                "references": line.references,
                "component_id": line.component_id,
                "requested": line.requested_quantity,
                "taken": line.taken_quantity,
                "shortfall": line.requested_quantity - line.taken_quantity,
                "sources": by_line.get(cast(int, line.id), []),
            }
            for line in lines
        ],
    }

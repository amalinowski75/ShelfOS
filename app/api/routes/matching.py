"""Matching-engine endpoint for the New Component dialog.

The dialog no longer works out types/parameters itself — the server does, via the
enrichment engine. This endpoint lets the dialog re-run the engine when the user picks
a different type, so its parameter fields refill for the chosen type. Mounted under the
protected routers (writer + CSRF), like the shop lookup it accompanies.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import (
    MatchProposalRead,
    MatchProposalRequest,
    ParameterValueSet,
)
from app.services.matching import MatchProposal, build_proposal
from app.services.shops.base import ProductData

router = APIRouter(prefix="/api/matching", tags=["matching"])


def proposal_read(proposal: MatchProposal) -> MatchProposalRead:
    """Turn an engine proposal into its API shape."""
    return MatchProposalRead(
        type_id=proposal.type_id,
        mounting_type=proposal.mounting_type,
        package=proposal.package,
        parameters=[
            ParameterValueSet(parameter_definition_id=pid, value=value)
            for pid, value in proposal.parameters
        ],
    )


@router.post("/proposal", response_model=MatchProposalRead)
def compute_proposal(
    payload: MatchProposalRequest, session: Session = Depends(get_session)
) -> MatchProposalRead:
    """Re-run the engine for a (user-chosen) type over the same product data."""
    product = ProductData(
        category=payload.category,
        shop_category=payload.shop_category,
        description=payload.description,
        package=payload.package,
        parameters=[(p.name, p.value) for p in payload.parameters],
    )
    proposal = build_proposal(session, product, type_id=payload.type_id)
    return proposal_read(proposal)

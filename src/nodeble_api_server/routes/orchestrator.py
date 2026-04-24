"""/api/v1/orchestrator/* — read-side access to the allocator's output.

Currently just one endpoint: GET /allocation. The desktop Market page
reads this to render global regime + per-strategy allocation breakdown
in one round-trip instead of mining card.allocation from
/strategies/{id}.

Why a dedicated endpoint:
- The Dashboard's per-strategy cards embed only *their own* allocation
  slice. Global fields (regime, composite_score, portfolio_nlv,
  account_profile) belong at the orchestrator level; pulling them from
  one arbitrary card's response would leak layer boundaries.
- Read-only, cheap — the underlying read_allocation() is already
  TTL-cached and used elsewhere. One new FastAPI route, zero new I/O.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.state_reader import read_allocation

router = APIRouter(
    prefix="/api/v1/orchestrator",
    dependencies=[Depends(require_bearer_token)],
)


@router.get("/allocation")
def get_allocation() -> dict:
    """Return ~/.nodeble-orchestrator/data/allocation.json as-is.

    The schema is whatever the orchestrator writes — api-server doesn't
    re-shape or validate. Consumers treat it as opaque JSON with known
    top-level fields (regime / composite_score / portfolio_nlv /
    strategies{} / account_profile / generated_at). Missing file → 404
    so the UI can show a "orchestrator 还未跑过" empty state instead of
    a confused partial render.
    """
    data = read_allocation()
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="allocation.json not found — run orchestrator first",
        )
    return data

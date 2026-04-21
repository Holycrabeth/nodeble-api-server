"""/api/v1/market/* routes.

`/market/status` feeds the Strategy Detail page's staleness banner — UI
wants to know whether edits we're about to accept will take effect now
(market open) or on the next session (market closed).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.market import get_market_status

router = APIRouter(
    prefix="/api/v1/market",
    dependencies=[Depends(require_bearer_token)],
)


@router.get("/status")
def market_status() -> dict:
    s = get_market_status()
    return {
        "is_open": s.is_open,
        "reason": s.reason,
        "next_open_iso": s.next_open_iso,
    }

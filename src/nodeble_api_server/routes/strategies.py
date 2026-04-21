"""/api/v1/strategies/* routes — Dashboard card data, one per strategy.

All routes require Bearer auth (attached at router level).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.logs import tail_bytes
from nodeble_api_server.state_reader import (
    STRATEGY_REGISTRY,
    build_strategy_card,
    list_installed_strategies,
    positions_as_list,
    read_allocation,
    read_config,
    read_state,
    strategy_log_path,
)

router = APIRouter(
    prefix="/api/v1/strategies",
    dependencies=[Depends(require_bearer_token)],
)


@router.get("")
def list_strategies() -> dict:
    installed = list_installed_strategies()
    return {"strategies": [build_strategy_card(sid) for sid in installed]}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str) -> dict:
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    card = build_strategy_card(strategy_id)
    card["config"] = read_config(strategy_id)
    allocation = read_allocation() or {}
    meta = STRATEGY_REGISTRY[strategy_id]
    alloc_key = meta.get("allocation_key", strategy_id)
    card["allocation"] = (allocation.get("strategies") or {}).get(alloc_key)
    return card


@router.get("/{strategy_id}/positions")
def get_positions(strategy_id: str) -> dict:
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    state = read_state(strategy_id)
    if state is None:
        return {"positions": []}
    return {"positions": positions_as_list(state.get("positions", []))}


@router.get("/{strategy_id}/logs")
def get_logs(
    strategy_id: str,
    cursor: int | None = None,
    limit: int = 200,
) -> dict:
    """Return a chunk of strategy log lines.

    - Unknown strategy id → 404.
    - Log file missing (strategy never ran, or log_path unconfigured) →
      200 with empty lines + cursor=0 so the UI can render an empty state
      instead of an error banner.
    - cursor None → initial reverse read of last `limit` lines from EOF.
    - cursor valid → incremental read from cursor to EOF.
    - cursor exceeds file size (rotate) → return initial read with
      truncated=True so the client resets its accumulated buffer.
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    path = strategy_log_path(strategy_id)
    if path is None:
        # Strategy exists but has no log_file mapping — same shape as
        # missing file, still 200 so UI stays consistent.
        return {"lines": [], "cursor": 0, "truncated": False}
    return tail_bytes(path, cursor, limit)

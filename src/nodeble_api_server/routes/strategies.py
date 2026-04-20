"""/api/v1/strategies/* routes — Dashboard card data, one per strategy.

All routes require Bearer auth (attached at router level).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.state_reader import (
    STRATEGY_REGISTRY,
    compute_health,
    count_active_positions,
    latest_log_mtime,
    list_installed_strategies,
    normalize_timestamp,
    positions_as_list,
    read_allocation,
    read_config,
    read_signal_timestamp,
    read_state,
    sum_active_budget,
)

router = APIRouter(
    prefix="/api/v1/strategies",
    dependencies=[Depends(require_bearer_token)],
)


def _build_card(strategy_id: str) -> dict:
    meta = STRATEGY_REGISTRY[strategy_id]
    state = read_state(strategy_id) or {}
    config = read_config(strategy_id) or {}
    allocation = read_allocation() or {}

    enabled = config.get("mode", "live") != "disabled"

    positions_raw = state.get("positions", {})
    open_positions = count_active_positions(positions_raw)
    budget_used = sum_active_budget(positions_raw)

    alloc_key = meta.get("allocation_key", strategy_id)
    alloc_entry = (allocation.get("strategies") or {}).get(alloc_key) or {}
    budget_max = (
        alloc_entry.get("max_buying_power")
        or (config.get("capital") or {}).get("budget")
        or 0
    )

    last_scan_at = normalize_timestamp(state.get("last_scan_date"))
    last_manage_at = normalize_timestamp(state.get("last_manage_date"))
    last_signal_at = read_signal_timestamp(strategy_id)

    log_mtime = None
    if not (last_scan_at and last_manage_at and last_signal_at):
        log_mtime = latest_log_mtime(strategy_id)
    last_scan_at = last_scan_at or log_mtime
    last_manage_at = last_manage_at or log_mtime
    last_signal_at = last_signal_at or log_mtime

    health = compute_health(last_scan_at, last_manage_at, last_signal_at)

    return {
        "id": strategy_id,
        "name": meta["name"],
        "enabled": enabled,
        "open_positions": open_positions,
        "budget_used": budget_used,
        "budget_max": budget_max,
        "last_signal_at": last_signal_at,
        "last_scan_at": last_scan_at,
        "last_manage_at": last_manage_at,
        "health": health,
        "version": None,
        "today_pnl": None,
        "cumulative_pnl_7d": None,
        "cumulative_pnl_30d": None,
        "circuit_breaker": None,
    }


@router.get("")
def list_strategies() -> dict:
    installed = list_installed_strategies()
    return {"strategies": [_build_card(sid) for sid in installed]}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str) -> dict:
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    card = _build_card(strategy_id)
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

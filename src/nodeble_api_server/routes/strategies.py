"""/api/v1/strategies/* routes — Dashboard card data, one per strategy.

All routes require Bearer auth (attached at router level).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel, Field

from nodeble_api_server.audit import write_event
from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.config_writer import run_shim
from nodeble_api_server.logs import tail_bytes
from nodeble_api_server.state_reader import (
    STRATEGY_REGISTRY,
    build_strategy_card,
    clear_cache,
    list_installed_strategies,
    positions_as_list,
    read_allocation,
    read_config,
    read_state,
    strategy_config_shim,
    strategy_log_path,
    strategy_venv_python,
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


class ValidatePayload(BaseModel):
    param_path: str = Field(min_length=1)
    new_value: Any


class CommitPayload(BaseModel):
    param_path: str = Field(min_length=1)
    new_value: Any
    reason: str = ""


def _resolve_shim(strategy_id: str) -> tuple[str, str]:
    """Return (shim_name, venv_python_str) or raise 404 / 422."""
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    shim = strategy_config_shim(strategy_id)
    if not shim:
        raise HTTPException(
            status_code=422,
            detail=f"Strategy {strategy_id} has no config shim registered",
        )
    venv = strategy_venv_python(strategy_id)
    if not venv or not venv.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Strategy {strategy_id} venv not found at {venv}",
        )
    return shim, str(venv)


@router.get("/{strategy_id}/config/editable-paths")
def editable_paths(strategy_id: str) -> dict:
    """Return the dotted config paths the UI is allowed to edit for this
    strategy. Frontend uses this to grey out / tooltip non-editable rows
    instead of letting users click ✎ only to get a 400 on validate.

    Implementation delegates to the shim's `list` action — each family
    knows its own whitelist (Group A reads yaml_path from *_PARAMS; B/C/D
    read the keys of our own whitelist dict)."""
    shim_name, venv_python = _resolve_shim(strategy_id)
    result = run_shim(
        venv_python=venv_python,  # type: ignore[arg-type]
        shim_name=shim_name,
        action="list",
        strategy_id=strategy_id,
        param_path="",  # unused by `list`
        value=None,
    )
    if not result.ok:
        raise HTTPException(status_code=500, detail=result.error or "list failed")
    paths = result.new if isinstance(result.new, list) else []
    return {"editable_paths": paths}


@router.post("/{strategy_id}/config/validate")
def validate_config(strategy_id: str, payload: ValidatePayload) -> dict:
    """Dry-run: run the shim's `validate` action without touching YAML.
    Validation failures are 200 with `{valid: false, error}` — HTTP errors
    are reserved for infrastructure issues (unknown strategy, shim missing,
    subprocess crash)."""
    shim_name, venv_python = _resolve_shim(strategy_id)
    result = run_shim(
        venv_python=venv_python,  # type: ignore[arg-type]
        shim_name=shim_name,
        action="validate",
        strategy_id=strategy_id,
        param_path=payload.param_path,
        value=payload.new_value,
    )
    if result.ok:
        return {
            "valid": True,
            "old_value": result.old,
            "normalized": result.new,
            "error": None,
        }
    return {
        "valid": False,
        "old_value": result.old,
        "normalized": None,
        "error": result.error or "validation failed",
    }


@router.put("/{strategy_id}/config")
def commit_config(strategy_id: str, payload: CommitPayload) -> dict:
    """Validate-then-set-then-audit. Any failure is recorded in
    audit.jsonl with the appropriate `result` category.

    Validate-twice pattern closes the TOCTOU window between "preview
    shows old value" and "write happens" — between our own calls, and
    between another client's concurrent edit."""
    shim_name, venv_python = _resolve_shim(strategy_id)

    # 1) Revalidate.
    pre = run_shim(
        venv_python=venv_python,  # type: ignore[arg-type]
        shim_name=shim_name,
        action="validate",
        strategy_id=strategy_id,
        param_path=payload.param_path,
        value=payload.new_value,
    )
    if not pre.ok:
        write_event(
            strategy=strategy_id,
            param_path=payload.param_path,
            old_value=pre.old,
            new_value=payload.new_value,
            reason=payload.reason,
            result="validation_failed",
            error=pre.error,
        )
        raise HTTPException(status_code=400, detail=pre.error or "validation failed")

    # 2) Set.
    set_result = run_shim(
        venv_python=venv_python,  # type: ignore[arg-type]
        shim_name=shim_name,
        action="set",
        strategy_id=strategy_id,
        param_path=payload.param_path,
        value=payload.new_value,
    )
    if not set_result.ok:
        err = set_result.error or ""
        result_category = "timeout" if "timed out" in err.lower() else "write_failed"
        write_event(
            strategy=strategy_id,
            param_path=payload.param_path,
            old_value=pre.old,
            new_value=payload.new_value,
            reason=payload.reason,
            result=result_category,
            error=err,
        )
        raise HTTPException(status_code=500, detail=err or "write failed")

    # 3) Audit success.
    write_event(
        strategy=strategy_id,
        param_path=payload.param_path,
        old_value=set_result.old,
        new_value=set_result.new,
        reason=payload.reason,
        result="success",
        error=None,
    )

    # 4) Invalidate state_reader's 5s TTL cache so the very next
    # GET /api/v1/strategies/{id} (triggered by the frontend's
    # invalidateQueries after a successful commit) sees the new YAML
    # value instead of the pre-write cached copy. clear_cache is
    # coarse-grained (drops everything) but cheap: the cache is 5s TTL
    # anyway; one forced re-read costs a couple of file stats.
    clear_cache()

    return {
        "committed": True,
        "old_value": set_result.old,
        "new_value": set_result.new,
    }


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

"""/api/v1/strategies/* routes — Dashboard card data, one per strategy.

All routes require Bearer auth (attached at router level).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel, Field

from datetime import datetime
from zoneinfo import ZoneInfo

from nodeble_api_server.actions import run_strategy_scan
from nodeble_api_server.audit import audit_path, write_event
from nodeble_api_server.audit_reader import read_audit_entries
from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.config_writer import run_shim
from dataclasses import asdict

from nodeble_api_server.history_reader import compute_since_date, read_pnl_entries
from nodeble_api_server.logs import read_recent_parsed_lines, tail_bytes
from nodeble_api_server.session_extractor import (
    extract_session_detail,
    extract_sessions,
)
from nodeble_api_server.positions_history_reader import (
    is_valid_date_format,
    read_available_dates,
    read_positions_at_date,
)
from nodeble_api_server.snapshot_writer import (
    positions_snapshot_path as _positions_snapshot_path,
    snapshot_path as _snapshot_path,
)
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


# Parameters the shim whitelists but the UI must NOT expose as a
# generic editable row. These have dedicated system endpoints that
# write them (killswitch for `mode`); letting the Config tab ✎ them
# directly would create two paths to flip the same knob with different
# audit semantics, and the UI-editor path would bypass the confirmation
# modal + reason-capture that dedicated endpoints enforce.
HIDDEN_FROM_CONFIG_EDITOR: frozenset[str] = frozenset({"mode"})


@router.get("/{strategy_id}/config/editable-paths")
def editable_paths(strategy_id: str) -> dict:
    """Return the dotted config paths the UI is allowed to edit for this
    strategy. Frontend uses this to grey out / tooltip non-editable rows
    instead of letting users click ✎ only to get a 400 on validate.

    Implementation delegates to the shim's `list` action — each family
    knows its own whitelist (Group A reads yaml_path from *_PARAMS; B/C/D
    read the keys of our own whitelist dict). We then filter out paths
    in HIDDEN_FROM_CONFIG_EDITOR: `mode` is shim-writable (killswitch
    needs the setter path) but must not surface as a ✎ row.
    """
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
    paths_raw = result.new if isinstance(result.new, list) else []
    paths = [p for p in paths_raw if p not in HIDDEN_FROM_CONFIG_EDITOR]
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


@router.get("/{strategy_id}/history/config")
def get_config_history(
    strategy_id: str,
    limit: int = 50,
    before_ts: str | None = None,
) -> dict:
    """Return audit.jsonl entries scoped to this strategy, newest first.

    - Unknown strategy id → 404 (matches other /strategies/{id}/* routes).
    - `limit` is clamped to [1, 200] so no caller can pull the whole log
      in one request.
    - `before_ts`: for pagination. Pass the `ts` of the oldest entry in
      the previous page to get the next older chunk.
    - `has_more` is True iff we returned exactly `limit` entries. A
      false positive here costs one empty "load more" request on the
      very rare boundary case — cheaper than counting total events.
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    capped_limit = max(1, min(limit, 200))
    entries = read_audit_entries(
        path=audit_path(),
        strategy=strategy_id,
        limit=capped_limit,
        before_ts=before_ts,
    )
    return {
        "entries": entries,
        "has_more": len(entries) == capped_limit,
    }


@router.get("/{strategy_id}/history/pnl")
def get_pnl_history(
    strategy_id: str,
    days: int = 30,
) -> dict:
    """Per-day cumulative realized PnL for the chart on the History tab.

    - Unknown strategy_id → 404.
    - `days` clamped to [1, 365]. `since_date = today - (days - 1)` so
      days=1 returns today only, days=30 returns a 30-calendar-day
      window inclusive.
    - Entries returned ascending by date (oldest first) so the chart
      renders left→right in time order.
    - `daily_delta` = cumulative[today] - cumulative[yesterday],
      null on the first row or whenever either cumulative is null.
    - Missing snapshot file (e.g. brand-new install pre-seed) → empty
      entries list. UI handles this as the "数据积累中" empty state.
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=404, detail=f"Unknown strategy: {strategy_id}"
        )
    days_clamped = max(1, min(days, 365))
    today = datetime.now(ZoneInfo("America/New_York")).date()
    since = compute_since_date(today, days_clamped)
    entries = read_pnl_entries(
        path=_snapshot_path(),
        strategy=strategy_id,
        since_date=since,
    )
    # Keep only the fields the chart needs (drop snapshot_at / budget
    # internals for a smaller wire payload — the detail tab has those
    # already via other routes).
    return {
        "strategy": strategy_id,
        "entries": [
            {
                "date": e["date"],
                "realized_pnl_cumulative": e.get("realized_pnl_cumulative"),
                "open_positions_count": e.get("open_positions_count", 0),
                "daily_delta": e.get("daily_delta"),
            }
            for e in entries
        ],
    }


@router.get("/{strategy_id}/history/positions")
def get_positions_history(
    strategy_id: str,
    date: str | None = None,
) -> dict:
    """Single-fetch endpoint for the Positions Replay card:
    - `available_dates`: newest-first list of snapshot dates (≤ 90 days)
      for the date-nav dropdown
    - Row for `date` (or the most-recent available if `date` is omitted):
      `snapshot_at` + raw `positions` array

    Unknown strategy_id → 404.
    Malformed `date` param → 400. Valid-but-missing date → 200 with
    empty positions + null snapshot_at (UI renders an empty state).
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=404, detail=f"Unknown strategy: {strategy_id}"
        )
    if date is not None and not is_valid_date_format(date):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format (expected YYYY-MM-DD): {date!r}",
        )

    path = _positions_snapshot_path()
    available = read_available_dates(path, strategy_id, days=90)
    resolved_date = date or (available[0] if available else None)

    if resolved_date is None:
        return {
            "strategy": strategy_id,
            "requested_date": None,
            "snapshot_at": None,
            "positions": [],
            "available_dates": [],
        }

    row = read_positions_at_date(path, strategy_id, resolved_date)
    return {
        "strategy": strategy_id,
        "requested_date": resolved_date,
        "snapshot_at": row.get("snapshot_at") if row else None,
        "positions": row.get("positions", []) if row else [],
        "available_dates": available,
    }


@router.get("/{strategy_id}/history/sessions")
def get_session_history(
    strategy_id: str,
    limit: int = 20,
    before_ts: str | None = None,
) -> dict:
    """Session list for the 运行历史 card on the History tab.

    One "session" = a contiguous cron run's log lines, grouped by
    sessionize-on-time-gap (default 180s). Returned newest-first.

    - Unknown strategy id → 404.
    - Missing log file (strategy never ran, or log_file unconfigured) →
      200 with empty sessions list — UI renders the empty state.
    - `limit` clamped to [1, 100]. `has_more` is True iff we returned
      exactly `limit` entries (same heuristic as /history/config).
    - `before_ts` pagination: pass the oldest returned session's
      start_ts to get the next older page.

    Reads up to the last ~1 MB of the log file (see
    logs.read_recent_parsed_lines); older sessions beyond that window
    are not paginate-able from this endpoint by design — the History
    tab only shows recent-history cadence, not archived logs.
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    path = strategy_log_path(strategy_id)
    if path is None:
        return {"sessions": [], "has_more": False}

    capped_limit = max(1, min(limit, 100))
    parsed = read_recent_parsed_lines(path)
    sessions = extract_sessions(parsed, limit=capped_limit, before_ts=before_ts)
    return {
        "sessions": [asdict(s) for s in sessions],
        "has_more": len(sessions) == capped_limit,
    }


@router.get("/{strategy_id}/history/sessions/detail")
def get_session_detail(
    strategy_id: str,
    start_ts: str,
    end_ts: str,
) -> dict:
    """Full parsed log-line list for a single session window.

    Called when the user expands a session in the accordion. Only the
    lines inside [start_ts, end_ts] come back, not the whole log file —
    a heavy session is still bounded by the session's own size.

    - Unknown strategy id → 404.
    - Missing log file → 200 with empty lines.
    - Lines without a parseable ts that follow an in-range line are
      included (Python traceback tails etc.); see
      session_extractor.extract_session_detail for the exact rule.
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    path = strategy_log_path(strategy_id)
    if path is None:
        return {"lines": []}

    parsed = read_recent_parsed_lines(path)
    lines = extract_session_detail(parsed, start_ts=start_ts, end_ts=end_ts)
    return {"lines": lines}


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


# ── Manual actions (M3.a): on-demand scan ───────────────────────────────────


class ScanActionRequest(BaseModel):
    """POST /strategies/{id}/actions/scan body.

    `mode`:
      - "dry_run" (MVP default): always passes `--dry-run` to the CLI.
        Safe to click — strategy computes candidates, prints decisions,
        doesn't hit the broker for placing orders.
      - "live": omits `--dry-run`. Subject to the strategy's yaml.mode.
        Requires `confirm=True` and a non-empty `reason` so accidental
        clicks can't fire live trades. (Enforced at route level, not
        the actions.py layer, so the core path stays simple.)

    `force=True` by default because a human pressed a button — they
    want it to run NOW, not skip because of a cron-gate like
    market-closed-time.

    `reason` is a short free-form string the operator types in the
    modal ("checking after Fed announcement", "verifying fix" etc.).
    Ends up in audit.jsonl alongside the request.
    """
    mode: str = Field("dry_run", pattern="^(dry_run|live)$")
    force: bool = True
    confirm: bool = False
    reason: str = Field("", max_length=500)


@router.post("/{strategy_id}/actions/scan")
def trigger_scan(strategy_id: str, payload: ScanActionRequest) -> dict:
    """Run `python -m <strategy-pkg> --mode scan [--dry-run] [--force]`
    once, right now, on the strategy's own venv. Returns stdout/stderr
    tails + duration + exit code so the operator sees what happened.

    Gates:
    - Unknown strategy_id → 404
    - mode="live" without confirm=True or empty reason → 400
      (we don't want a client bug or mis-typed body firing a live scan;
      the desktop modal makes the operator type "LIVE SCAN" to set both)

    Audit: one entry with param_path="actions.scan", new_value carries
    the request + result summary. Reuses the existing audit schema
    rather than inventing a parallel action log — simpler reader code,
    single time-ordered trail.
    """
    if strategy_id not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")

    if payload.mode == "live":
        if not payload.confirm:
            raise HTTPException(
                status_code=400,
                detail="live scan requires confirm=true",
            )
        if not payload.reason.strip():
            raise HTTPException(
                status_code=400,
                detail="live scan requires a non-empty reason",
            )

    # Dispatch to actions.py — the heavy lifting (subprocess, timeout,
    # stdout capture, cache invalidation) lives there so this route stays
    # focused on HTTP concerns.
    result = run_strategy_scan(
        strategy_id,
        mode=payload.mode,
        force=payload.force,
    )

    # Audit the action. The schema is config-centric (param_path / old /
    # new) but carries actions cleanly: param_path namespaces the action
    # type, new_value packs request + result summary. Kept old_value=None
    # to distinguish from config edits.
    audit_new_value = {
        "request": {
            "mode": payload.mode,
            "force": payload.force,
        },
        "result": {
            "status": result.status,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        },
    }
    try:
        write_event(
            strategy=strategy_id,
            param_path="actions.scan",
            old_value=None,
            new_value=audit_new_value,
            reason=payload.reason.strip() or "manual scan",
            result=result.status,
            error=result.error,
        )
    except OSError as e:
        # Audit write is best-effort for actions — losing the audit
        # record is worse than dropping the response, so we log the
        # error via the result and still return. (Config edits treat
        # audit failure as fatal because the change landed; a scan
        # producing decisions is transient — next scan re-derives.)
        result = ScanResult_with_audit_warn(result, f"audit write failed: {e}")

    return asdict(result)


def ScanResult_with_audit_warn(result, warn: str):
    """Append an audit warning to the error field without losing the
    original error. Separate helper so the happy path stays readable."""
    from nodeble_api_server.actions import ScanResult
    combined = result.error or ""
    combined = f"{combined}; {warn}" if combined else warn
    return ScanResult(
        status=result.status,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
        started_at=result.started_at,
        completed_at=result.completed_at,
        error=combined,
    )

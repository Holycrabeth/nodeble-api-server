"""/api/v1/server/* routes — GUI v1 install wizard backend.

Phase A Week 1 stubs per Phase 4.1 contract freeze
(`~/projects/cto/reviews/2026-04-26-phase-4.1-backend-contract-freeze.md`).

13 endpoints across 5 categories. All return spec-exact shapes so
UI 总监 frontend can hit real backend (replacing msw mocks). Real
implementation follows in Week 2-3 (install orchestrator + SSE wiring +
deploy.sh subprocess invocation per ARCH-18 §2 contract).

All endpoints require Bearer token (router-level dependency).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel, Field

from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.state_reader import STRATEGY_REGISTRY


router = APIRouter(
    prefix="/api/v1/server",
    dependencies=[Depends(require_bearer_token)],
)


# ── In-memory install state (Phase A Week 1 stub) ───────────────────────────
#
# Real Phase A Week 2-3 swaps this for asyncio + JSON persistence per
# Q4 decision in Phase 4.1 contract freeze §6. For Week 1 stub, in-memory
# dict is sufficient — api-server restart resets state, which is fine for
# UI 总监 dev work (they don't depend on persistence yet).

_INSTALL_STATE: dict[str, dict] = {}
# install_id → {strategy, status, started_at, completed_at, current_step,
#               steps_completed, log_tail, error}


_TIGER_CREDS_STUB: dict[str, Any] = {"exists": False, "account": None, "stored_at": None}


def _utc_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ── Discovery (2 endpoints) ─────────────────────────────────────────────────


@router.get("/installed-strategies")
def get_installed_strategies() -> dict:
    """List strategies + install status. Stub: all 9 from STRATEGY_REGISTRY,
    all status='not_installed' with no version. Real impl reads disk.

    Contract: Phase 4.1 freeze §1.1.
    """
    home = Path.home()
    cards = []
    for sid, meta in STRATEGY_REGISTRY.items():
        # Detect install via folder presence (real impl in Week 2)
        installed = (home / meta["folder"] / "data" / "state.json").exists()
        cards.append({
            "id": sid,
            "name": meta["name"],
            "installed": installed,
            "status": "running" if installed else "not_installed",
            "installed_at": None,  # stub — Week 2 reads from deploy log
            "version": None,        # stub — Week 2 reads from pyproject or VERSION file
            "latest_version_available": None,  # stub — fetched from manifest in Week 2
        })
    return {
        "strategies": cards,
        "fetched_at": _utc_iso(),
    }


@router.get("/strategy-versions")
def get_strategy_versions() -> dict:
    """Fetch release manifest for update checks. Stub returns canonical
    placeholder since real manifest lives at https://nodeble.app/releases.json
    (already deployed Phase B pre-ship 2026-04-26). Week 2 wires real fetch
    + 5 min cache.
    """
    return {
        "manifest_url": "https://nodeble.app/releases.json",
        "fetched_at": _utc_iso(),
        "manifest_unreachable": False,
        "strategies": {
            sid: {
                "latest": "0.0.0",
                "released_at": "2026-04-26T20:00:00Z",
                "changelog_url": f"https://github.com/Holycrabeth/{meta['repo_dir'].split('/')[-1]}/releases",
            }
            for sid, meta in STRATEGY_REGISTRY.items()
        },
    }


# ── Lifecycle (5 endpoints + 1 validate) ────────────────────────────────────


class InstallRequest(BaseModel):
    install_id: str = Field(min_length=1)
    config: dict = Field(default_factory=dict)
    telegram: dict | None = None
    reuse_tiger_creds: bool = True


class ValidateRequest(BaseModel):
    config: dict = Field(default_factory=dict)
    telegram: dict | None = None
    reuse_tiger_creds: bool = True


def _validate_strategy(strategy: str) -> None:
    if strategy not in STRATEGY_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy}")


@router.post("/install/{strategy}", status_code=202)
def post_install(strategy: str, payload: InstallRequest) -> dict:
    """Spawn install (stub: stores state, real impl Week 2 invokes deploy.sh).

    Per contract §1.2: enforce mode=dry_run server-side regardless of payload.
    """
    _validate_strategy(strategy)

    # CRITICAL — enforce dry_run default per UI 总监 Gap 2 catch
    # Real impl Week 2: this writes to the strategy.yaml file before subprocess
    config = dict(payload.config)
    config["mode"] = "dry_run"

    install_id = payload.install_id
    if install_id in _INSTALL_STATE:
        # Idempotency — same install_id POSTed twice returns existing
        existing = _INSTALL_STATE[install_id]
        return {
            "install_id": install_id,
            "status": existing.get("status", "queued"),
            "sse_url": f"/api/v1/server/install/{install_id}/stream",
            "status_url": f"/api/v1/server/install/{install_id}/status",
            "log_url": f"/api/v1/server/install/{install_id}/log",
            "started_at": existing.get("started_at"),
        }

    started_at = _utc_iso()
    _INSTALL_STATE[install_id] = {
        "strategy": strategy,
        "status": "queued",
        "started_at": started_at,
        "completed_at": None,
        "current_step": "Validating config",
        "steps_completed": [],
        "log_tail": [
            {"level": "info", "message": f"Install {install_id} queued for {strategy}", "ts": started_at},
            {"level": "info", "message": "(Stub Phase A Week 1 — real orchestrator Week 2)", "ts": started_at},
        ],
        "error": None,
        "config_with_mode_dry_run_enforced": config,  # diagnostic
    }

    return {
        "install_id": install_id,
        "status": "queued",
        "sse_url": f"/api/v1/server/install/{install_id}/stream",
        "status_url": f"/api/v1/server/install/{install_id}/status",
        "log_url": f"/api/v1/server/install/{install_id}/log",
        "started_at": started_at,
    }


@router.post("/install/{strategy}/validate")
def post_install_validate(strategy: str, payload: ValidateRequest) -> dict:
    """Pre-flight validate without spawning subprocess. Stub returns
    valid=true always; real impl Week 2 runs config schema check per
    strategy.
    """
    _validate_strategy(strategy)
    return {
        "valid": True,
        "errors": [],
        "warnings": [],
    }


@router.post("/uninstall/{strategy}")
def post_uninstall(strategy: str) -> dict:
    """Remove install (stub: returns ok). Real impl Week 2 stops bot
    service, removes cron, archives state. 409 if open positions.
    """
    _validate_strategy(strategy)
    # Stub: real impl checks state.json for open positions, returns 409 if any
    return {
        "status": "uninstalled",
        "uninstalled_at": _utc_iso(),
        "state_archive_path": f"~/.nodeble-pnl/data/state_archive/{strategy}/{datetime.now().strftime('%Y-%m-%d')}.json",
    }


class UpdateRequest(BaseModel):
    install_id: str = Field(min_length=1)
    target_version: str | None = None


@router.post("/update/{strategy}", status_code=202)
def post_update(strategy: str, payload: UpdateRequest) -> dict:
    """Update strategy. Reuses install orchestrator. Stub returns 202
    like install.
    """
    _validate_strategy(strategy)

    install_id = payload.install_id
    started_at = _utc_iso()
    _INSTALL_STATE[install_id] = {
        "strategy": strategy,
        "status": "queued",
        "started_at": started_at,
        "completed_at": None,
        "current_step": "Updating",
        "steps_completed": [],
        "log_tail": [
            {"level": "info", "message": f"Update {install_id} queued for {strategy}", "ts": started_at},
        ],
        "error": None,
        "operation": "update",
        "target_version": payload.target_version,
    }
    return {
        "install_id": install_id,
        "status": "queued",
        "sse_url": f"/api/v1/server/install/{install_id}/stream",
        "status_url": f"/api/v1/server/install/{install_id}/status",
        "log_url": f"/api/v1/server/install/{install_id}/log",
        "started_at": started_at,
    }


@router.post("/pause/{strategy}")
def post_pause(strategy: str) -> dict:
    """Disable scanner cron. Stub returns paused. Real impl Week 2
    edits crontab.
    """
    _validate_strategy(strategy)
    return {
        "status": "paused",
        "paused_at": _utc_iso(),
        "cron_disabled": ["signal", "scan"],
    }


@router.post("/resume/{strategy}")
def post_resume(strategy: str) -> dict:
    """Re-enable scanner cron. Stub returns running."""
    _validate_strategy(strategy)
    return {
        "status": "running",
        "resumed_at": _utc_iso(),
    }


# ── Tiger creds (2 endpoints) ───────────────────────────────────────────────


class TigerCredsRequest(BaseModel):
    tiger_id: str = Field(min_length=1)
    tiger_account: str = Field(min_length=1)
    private_key_pem: str = Field(min_length=1)


@router.put("/credentials/tiger")
def put_tiger_creds(payload: TigerCredsRequest) -> dict:
    """Store Tiger creds. Stub records exists=true in memory.
    Real impl Week 2 writes ~/.nodeble-api/secrets/tiger.yaml (mode 0600).

    Multipart .properties upload is alternative shape — added Week 2.
    """
    global _TIGER_CREDS_STUB
    _TIGER_CREDS_STUB = {
        "exists": True,
        "account": payload.tiger_account,
        "stored_at": _utc_iso(),
    }
    return {
        "status": "stored",
        "account": payload.tiger_account,
        "stored_at": _TIGER_CREDS_STUB["stored_at"],
    }


@router.get("/credentials/tiger")
def get_tiger_creds() -> dict:
    """Check creds presence WITHOUT leaking secret. Stub reads in-memory."""
    return {
        "exists": _TIGER_CREDS_STUB["exists"],
        "account": _TIGER_CREDS_STUB["account"],
        "stored_at": _TIGER_CREDS_STUB["stored_at"],
    }


# ── Install observability (3 endpoints) ─────────────────────────────────────


def _mock_install_steps() -> list[dict]:
    """Canned step sequence for Wheel install demo (Phase A Week 1 stub).

    Real Week 2 reads stdout/stderr from deploy.sh subprocess and emits
    real events. For now this is a deterministic sequence.
    """
    return [
        {"step": "Validating config", "duration_ms": 320},
        {"step": "Cloning repo", "duration_ms": 1200},
        {"step": "Setting up venv", "duration_ms": 4500},
        {"step": "Installing dependencies", "duration_ms": 8000},
        {"step": "Resolving Tiger credentials", "duration_ms": 200},
        {"step": "Writing strategy.yaml (mode=dry_run enforced)", "duration_ms": 100},
        {"step": "Setting up cron jobs", "duration_ms": 300},
        {"step": "Starting bot service", "duration_ms": 600},
        {"step": "Smoke test (dry-run scan)", "duration_ms": 5000},
        {"step": "Install complete", "duration_ms": 100},
    ]


async def _sse_event(event: str, data: dict) -> str:
    """Format SSE event per Phase 4.1 contract §2.1."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _generate_install_events(install_id: str):
    """Async generator yielding mock install progress events.

    Phase A Week 1: deterministic 10-step Wheel install simulation,
    ~3s total wall-clock. Real Week 2 wires to subprocess stdout.
    """
    state = _INSTALL_STATE.get(install_id)
    if state is None:
        # Late subscribers / unknown install_id — emit synthesized 'unknown' completion
        yield await _sse_event("complete", {
            "status": "failed",
            "duration_ms": 0,
            "error": "install_id not found",
            "ts": _utc_iso(),
        })
        return

    state["status"] = "running"
    started_t = time.monotonic()
    steps = _mock_install_steps()

    for step_def in steps:
        step_name = step_def["step"]
        # in_progress
        in_prog_event = {
            "step": step_name,
            "status": "in_progress",
            "ts": _utc_iso(),
        }
        state["current_step"] = step_name
        yield await _sse_event("step", in_prog_event)

        # Simulate work (cap at 300ms in stub so frontend dev iteration is fast)
        await asyncio.sleep(min(step_def["duration_ms"] / 1000.0, 0.3))

        # ok
        ok_event = {
            "step": step_name,
            "status": "ok",
            "duration_ms": step_def["duration_ms"],
            "ts": _utc_iso(),
        }
        state["steps_completed"].append(ok_event)
        state["log_tail"].append({
            "level": "info",
            "message": f"Step '{step_name}' completed in {step_def['duration_ms']}ms",
            "ts": ok_event["ts"],
        })
        yield await _sse_event("step", ok_event)

    # complete
    completed_at = _utc_iso()
    elapsed_ms = int((time.monotonic() - started_t) * 1000)
    complete_event = {
        "status": "success",
        "duration_ms": elapsed_ms,
        "ts": completed_at,
    }
    state["status"] = "success"
    state["completed_at"] = completed_at
    yield await _sse_event("complete", complete_event)


@router.get("/install/{install_id}/stream")
async def get_install_stream(install_id: str) -> StreamingResponse:
    """SSE stream of install progress events.

    Phase A Week 1: replays canned 10-step sequence. Real Week 2 wires
    to subprocess output + persisted event log so reconnect/replay work.
    """
    return StreamingResponse(
        _generate_install_events(install_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx-style buffering
        },
    )


@router.get("/install/{install_id}/status")
def get_install_status(install_id: str) -> dict:
    """Polling fallback for SSE — returns current state + log tail."""
    state = _INSTALL_STATE.get(install_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"install_id not found: {install_id}")

    return {
        "install_id": install_id,
        "status": state["status"],
        "current_step": state.get("current_step"),
        "steps_completed": state.get("steps_completed", []),
        "log_tail": state.get("log_tail", [])[-100:],
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "error": state.get("error"),
    }


@router.get("/install/{install_id}/log")
def get_install_log(install_id: str) -> PlainTextResponse:
    """Full text log of install. Used by frontend View logs link on failure.

    Returns text/plain (not JSON wrapper).
    """
    state = _INSTALL_STATE.get(install_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"install_id not found: {install_id}")

    lines = [
        f"[{e['ts']}] [{e['level'].upper()}] {e['message']}"
        for e in state.get("log_tail", [])
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


# ── Server logs (1 endpoint) ────────────────────────────────────────────────


@router.get("/logs/api-server")
def get_api_server_logs(lines: int = 200, level: str | None = None) -> dict:
    """Recent api-server systemd journal lines.

    Phase A Week 1 stub: returns canned recent events. Real Week 2
    invokes journalctl + parses.
    """
    if lines < 1 or lines > 500:
        raise HTTPException(status_code=400, detail="lines must be 1-500")
    if level is not None and level not in ("info", "warn", "error"):
        raise HTTPException(status_code=400, detail="level must be info|warn|error")

    # Stub canned data — real impl Week 2
    canned = [
        {"ts": _utc_iso(), "level": "info", "message": "api-server started"},
        {"ts": _utc_iso(), "level": "info", "message": "Bearer auth middleware active"},
        {"ts": _utc_iso(), "level": "info", "message": "Phase A Week 1 server stubs serving (this is a stub log line)"},
    ]
    if level:
        canned = [e for e in canned if e["level"] == level]
    return {
        "lines": canned[-lines:],
        "total_returned": min(len(canned), lines),
        "source": "stub_phase_a_week1",
    }

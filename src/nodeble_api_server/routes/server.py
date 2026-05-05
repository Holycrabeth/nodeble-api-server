"""/api/v1/server/* routes — GUI v1 install wizard backend.

Phase A Week 1 stubs per Phase 4.1 contract freeze
(`~/projects/cto/reviews/2026-04-26-phase-4.1-backend-contract-freeze.md`).

Phase A Week 3 wiring (Path C 5/5 — `2026-05-05-path-c-saas-install-master-spec.md`):
- ``post_install`` + ``post_update`` schedule ``install_runner.run_install``
  as background asyncio task; mock 10-step generator removed
- ``_generate_install_events`` is now a real replay-events.jsonl + tail
  loop, keyed off the events.jsonl that install_runner writes
- ``/logs/api-server`` wraps ``journalctl --user`` + reuses
  ``logs.parse_log_line`` for line shape consistency

Items 4 of dispatch (lifecycle endpoints — pause/resume/uninstall/update
crontab edits) deferred to next dispatch pending L1 §7.11 #8 cron-edit
permission carve-out from CEO. Stubs preserved here.

All endpoints require Bearer token (router-level dependency).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
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
from nodeble_api_server import (
    crontab_ops,
    install_runner,
    install_state,
    logs as logs_module,
    release_manifest,
    tiger_creds,
)


router = APIRouter(
    prefix="/api/v1/server",
    dependencies=[Depends(require_bearer_token)],
)


# Phase A Week 2: install state persisted to disk via install_state module.
# Tiger creds persisted to disk via tiger_creds module.
# Release manifest fetched (cached 5 min) via release_manifest module.
#
# Backwards-compat: the legacy in-memory _INSTALL_STATE dict is kept as a
# Week 1 reference + retained for any tests that imported it. Real reads /
# writes go through install_state module functions.

_INSTALL_STATE: dict[str, dict] = {}
_TIGER_CREDS_STUB: dict[str, Any] = {"exists": False, "account": None, "stored_at": None}

# Module-level set holding background install tasks. Without an active
# reference the asyncio task can be garbage-collected mid-run; this set
# pins each task until its done-callback removes it.
_RUNNING_INSTALL_TASKS: set[asyncio.Task] = set()

# A11 fix (CTO ticket 2026-05-05): unit name was `api-server.service` but
# Tower verify-from-source confirms real systemd --user unit is named
# `nodeble-api-server.service`. The typo silently returned `lines: []` from
# /logs/api-server because journalctl was queried for a non-existent unit.
# Single source of truth: crontab_ops.DEFAULT_API_SERVER_UNIT — both
# /logs/api-server (journalctl) and lifecycle endpoints (process binding
# check) use the same constant.
_API_SERVER_SYSTEMD_UNIT = "nodeble-api-server.service"


def _utc_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_deploy_root(strategy: str) -> Path:
    """``~/projects/<repo_dir>/`` for the strategy.

    ``repo_dir`` comes from ``state_reader.STRATEGY_REGISTRY`` (e.g.
    ``"projects/nodeble-wheel"``). ``Path.home()`` resolved per-call so
    test ``monkeypatch.setattr(Path, 'home', ...)`` works.
    """
    meta = STRATEGY_REGISTRY.get(strategy)
    if not meta:
        # _validate_strategy below catches this earlier in normal flow.
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy}")
    return Path.home() / meta["repo_dir"]


def _write_install_config(install_id: str, config: dict) -> Path:
    """Write the config dict as JSON to the install dir (next to state.json).

    Lives in the install_state's per-install dir so it persists for
    diagnostics + survives api-server restart. Returns the path passed
    to deploy.sh's ``--config`` flag.
    """
    install_dir = (
        Path.home() / ".nodeble-api" / "data" / "installs" / install_id
    )
    install_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = install_dir / "config.json"
    cfg_path.write_text(json.dumps(config, indent=2))
    return cfg_path


def _spawn_install_runner(
    install_id: str,
    strategy: str,
    config: dict,
    extra_args: list[str] | None = None,
) -> None:
    """Schedule ``install_runner.run_install`` as a background asyncio task.

    Pins the task in ``_RUNNING_INSTALL_TASKS`` so it survives garbage
    collection until completion. Called from ``post_install`` and
    ``post_update`` — both are async so an event loop is available.
    """
    deploy_root = _resolve_deploy_root(strategy)
    config_json_path = _write_install_config(install_id, config)
    cmd = install_runner.build_deploy_cmd(
        strategy=strategy,
        config_json_path=config_json_path,
        deploy_root=deploy_root,
        extra_args=extra_args,
    )
    task = asyncio.create_task(
        install_runner.run_install(
            install_id=install_id,
            cmd=cmd,
            cwd=deploy_root,
        )
    )
    _RUNNING_INSTALL_TASKS.add(task)
    task.add_done_callback(_RUNNING_INSTALL_TASKS.discard)


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
    """Fetch release manifest from https://nodeble.app/releases.json.

    Cached 5 min via release_manifest module. Returns spec-exact response
    even on fetch failure (manifest_unreachable=true + empty/stale strategies).
    """
    return release_manifest.fetch()


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
async def post_install(strategy: str, payload: InstallRequest) -> dict:
    """Spawn install via ``install_runner`` background task. Persisted state on disk.

    Per contract §1.2: enforce mode=dry_run server-side regardless of payload
    (UI 总监 Gap 2 fix). Idempotent: same install_id returns existing without
    re-spawning the subprocess (install_state.create returns existing state).

    Pre-spawn validation per UI 总监 Bug 1 audit (2026-04-29):
      reuse_tiger_creds=true + Tiger creds NOT on disk → 422 fail-fast
      (was: 202 + subprocess fail at "Resolving Tiger credentials" 30s later =
      bad UX).

    Phase A Week 3 wiring (Path C 5/5): post-install_state.create, schedules
    ``install_runner.run_install`` as background task. SSE stream / status
    endpoints read events.jsonl + state.json that the runner writes.
    """
    _validate_strategy(strategy)

    # Bug 1 gate: reuse_tiger_creds=true requires creds already on disk.
    if payload.reuse_tiger_creds:
        creds = tiger_creds.summary()
        if not creds.get("exists"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Tiger credentials not found on server. "
                    "Either PUT /api/v1/server/credentials/tiger first, "
                    "or set reuse_tiger_creds=false and include creds in config payload."
                ),
            )

    # CRITICAL — enforce dry_run default per UI 总监 Gap 2 catch
    config = dict(payload.config)
    config["mode"] = "dry_run"

    install_id = payload.install_id
    # Idempotent: if install_id already exists, returns existing state without
    # creating a new install dir (so we don't re-spawn the subprocess below).
    existing = install_state.read(install_id)
    state = install_state.create(
        install_id=install_id,
        strategy=strategy,
        config=config,
    )
    is_new_install = existing is None

    # Mirror to in-memory _INSTALL_STATE for backward-compat with Week 1 tests
    _INSTALL_STATE[install_id] = state

    # Phase A Week 3: spawn the real install_runner subprocess for new installs.
    # Idempotent re-POSTs of the same install_id do NOT re-spawn (state already
    # captures the prior run's outcome).
    if is_new_install:
        extra_args: list[str] = []
        if payload.telegram is None:
            # No Telegram config provided → skip Telegram setup in deploy.sh
            extra_args.append("--skip-telegram")
        _spawn_install_runner(install_id, strategy, config, extra_args=extra_args)

    return {
        "install_id": install_id,
        "status": state.get("status", "queued"),
        "sse_url": f"/api/v1/server/install/{install_id}/stream",
        "status_url": f"/api/v1/server/install/{install_id}/status",
        "log_url": f"/api/v1/server/install/{install_id}/log",
        "started_at": state.get("started_at"),
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
    """Remove strategy's cron lines (Path C Item 4 — Phase A follow-up).

    Crontab editing goes through ``crontab_ops.uninstall_strategy_cron``
    which enforces the L1 §7.11 #8 4-constraint contract (process binding
    check, path+module scope, pre-edit backup, post-edit verification).

    On constraint failure → 422 with diagnostic. Other failures → 500.

    Note: this endpoint currently handles **cron removal only**. Stopping
    the systemd --user bot service + archiving state.json + open-position
    409 gating remain in their Week 1 stubs (deferred to a separate
    follow-up PR — frontend currently only uses pause/resume; hard
    uninstall waits on the wider lifecycle UX from 前端总监).
    """
    _validate_strategy(strategy)
    # install_id encodes the action moment; reuses the install_state pattern
    # for pre-edit backup naming.
    install_id = f"uninstall-{strategy}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = crontab_ops.uninstall_strategy_cron(strategy, install_id)
    if not result["ok"]:
        if result.get("error") == "process_binding_check_failed":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": result["error"],
                    "diagnostic": result.get("diagnostic"),
                },
            )
        raise HTTPException(
            status_code=500,
            detail={
                "error": result.get("error"),
                "backup_path": result.get("backup_path"),
                "restored_from_backup": result.get("restored_from_backup"),
            },
        )
    return {
        "status": "uninstalled",
        "uninstalled_at": _utc_iso(),
        "lines_changed": result["lines_changed"],
        "backup_path": result["backup_path"],
        # state_archive_path remains a TODO until state-archiving wiring lands;
        # frontend should not depend on this field yet.
        "state_archive_path": None,
    }


class UpdateRequest(BaseModel):
    install_id: str = Field(min_length=1)
    target_version: str | None = None


@router.post("/update/{strategy}", status_code=202)
async def post_update(strategy: str, payload: UpdateRequest) -> dict:
    """Update strategy. Reuses install_runner with operation='update'.

    Phase A Week 3 wiring: same subprocess pattern as install — invokes
    ``deploy.sh --non-interactive --config <existing>``. deploy.sh's
    idempotency contract (§5) handles "already at target version" →
    ``STATUS: already_installed``. Per-strategy version pinning is
    deploy.sh's responsibility (read from RESULT_VERSION on prior install
    or fall through to module's master branch).
    """
    _validate_strategy(strategy)

    install_id = payload.install_id
    existing = install_state.read(install_id)
    state = install_state.create(
        install_id=install_id,
        strategy=strategy,
        config={},  # update doesn't take new config — uses existing strategy.yaml
        operation="update",
        target_version=payload.target_version,
    )
    is_new_update = existing is None

    _INSTALL_STATE[install_id] = state

    if is_new_update:
        _spawn_install_runner(install_id, strategy, config={}, extra_args=["--skip-telegram"])

    return {
        "install_id": install_id,
        "status": state.get("status", "queued"),
        "sse_url": f"/api/v1/server/install/{install_id}/stream",
        "status_url": f"/api/v1/server/install/{install_id}/status",
        "log_url": f"/api/v1/server/install/{install_id}/log",
        "started_at": state.get("started_at"),
    }


@router.post("/pause/{strategy}")
def post_pause(strategy: str) -> dict:
    """Comment-out strategy's cron lines (Path C Item 4 — Phase A follow-up).

    Adds ``# PAUSED-by-api: `` prefix to every in-scope line so cron
    treats them as comments. ``resume`` strips the prefix.

    Crontab editing goes through ``crontab_ops.pause_strategy`` which
    enforces the L1 §7.11 #8 4-constraint contract (process binding
    check, path+module scope, pre-edit backup, post-edit verification).
    """
    _validate_strategy(strategy)
    install_id = f"pause-{strategy}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = crontab_ops.pause_strategy(strategy, install_id)
    if not result["ok"]:
        if result.get("error") == "process_binding_check_failed":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": result["error"],
                    "diagnostic": result.get("diagnostic"),
                },
            )
        raise HTTPException(
            status_code=500,
            detail={
                "error": result.get("error"),
                "backup_path": result.get("backup_path"),
                "restored_from_backup": result.get("restored_from_backup"),
            },
        )
    return {
        "status": "paused",
        "paused_at": _utc_iso(),
        "lines_changed": result["lines_changed"],
        "backup_path": result["backup_path"],
        # cron_disabled list preserved for backward-compat with frontend that
        # may show "Signal/Scan disabled" badges — populated heuristically
        # since real-time cron-line introspection is not implemented yet.
        "cron_disabled": ["signal", "scan"],
    }


@router.post("/resume/{strategy}")
def post_resume(strategy: str) -> dict:
    """Strip ``# PAUSED-by-api: `` prefix from strategy's cron lines.

    See ``post_pause`` for details. Same 4-constraint contract enforcement.
    """
    _validate_strategy(strategy)
    install_id = f"resume-{strategy}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = crontab_ops.resume_strategy(strategy, install_id)
    if not result["ok"]:
        if result.get("error") == "process_binding_check_failed":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": result["error"],
                    "diagnostic": result.get("diagnostic"),
                },
            )
        raise HTTPException(
            status_code=500,
            detail={
                "error": result.get("error"),
                "backup_path": result.get("backup_path"),
                "restored_from_backup": result.get("restored_from_backup"),
            },
        )
    return {
        "status": "running",
        "resumed_at": _utc_iso(),
        "lines_changed": result["lines_changed"],
        "backup_path": result["backup_path"],
    }


# ── Tiger creds (2 endpoints) ───────────────────────────────────────────────


class TigerCredsRequest(BaseModel):
    tiger_id: str = Field(min_length=1)
    tiger_account: str = Field(min_length=1)
    private_key_pem: str = Field(min_length=1)


@router.put("/credentials/tiger")
def put_tiger_creds(payload: TigerCredsRequest) -> dict:
    """Store Tiger creds to ~/.nodeble-api/secrets/tiger.yaml (mode 0600).

    Atomic write via tempfile + os.replace. Survives api-server restart.
    Multipart .properties upload alternative shape: deferred to v1.5.
    """
    return tiger_creds.store(
        tiger_id=payload.tiger_id,
        tiger_account=payload.tiger_account,
        private_key_pem=payload.private_key_pem,
    )


@router.get("/credentials/tiger")
def get_tiger_creds() -> dict:
    """Check creds presence WITHOUT leaking private key.

    Returns {exists, account, stored_at}. Reads from disk
    (~/.nodeble-api/secrets/tiger.yaml).
    """
    return tiger_creds.summary()


# ── Install observability (3 endpoints) ─────────────────────────────────────


async def _sse_event(event: str, data: dict) -> str:
    """Format SSE event per Phase 4.1 contract §2.1."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# SSE tail polling — read interval + hard cap. The hard cap matches
# install_runner's default total budget so a runaway subprocess never
# leaves an SSE stream open forever; install_runner itself emits a
# terminal complete on its own timeout, but this is belt-and-suspenders.
_SSE_TAIL_POLL_INTERVAL_S = 0.5
_SSE_TAIL_MAX_WAIT_S = 660  # install_runner budget 600s + 60s slack


async def _generate_install_events(install_id: str):
    """Async generator yielding install progress events from events.jsonl.

    Phase A Week 3 (Path C 5/5): the events.jsonl file is the single
    source of truth, written exclusively by ``install_runner`` running
    as a background asyncio task spawned by ``post_install`` /
    ``post_update``. This generator is a pure consumer: replays whatever's
    already there, then polls for new entries until either a terminal
    ``complete`` event arrives OR the hard wait cap is hit.

    Late subscribers / SSE reconnects: full replay first so the client
    catches up to the current state, then live tail.

    Unknown install_id: synthetic ``complete`` with status=failed so the
    UI can render an error state instead of hanging.
    """
    state = install_state.read(install_id)
    if state is None:
        yield await _sse_event("complete", {
            "status": "failed",
            "duration_ms": 0,
            "error": "install_id not found",
            "ts": _utc_iso(),
        })
        return

    # Phase 1: replay everything already persisted.
    seen_count = 0
    saw_complete = False
    for event in install_state.replay_events(install_id):
        yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
        seen_count += 1
        if event["event"] == "complete":
            saw_complete = True
            return

    # If state is already terminal but no complete event in jsonl (edge case
    # from prior shipped runs without complete event), synthesize one.
    if not saw_complete and state.get("status") in install_state.TERMINAL_STATUSES:
        yield await _sse_event("complete", {
            "status": state["status"],
            "duration_ms": 0,
            "error": state.get("error"),
            "ts": state.get("completed_at") or _utc_iso(),
        })
        return

    # Phase 2: tail the events.jsonl until terminal complete OR max wait.
    # Polling at 0.5s is fine for install UX (install steps take seconds-
    # to-minutes); reduces complexity vs inotify or aiofiles tailing.
    waited_s = 0.0
    while waited_s < _SSE_TAIL_MAX_WAIT_S:
        await asyncio.sleep(_SSE_TAIL_POLL_INTERVAL_S)
        waited_s += _SSE_TAIL_POLL_INTERVAL_S
        all_events = list(install_state.replay_events(install_id))
        new_events = all_events[seen_count:]
        for event in new_events:
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
            seen_count += 1
            if event["event"] == "complete":
                return

    # Hard timeout — install_runner's own budget should have fired by now.
    # Emit a synthetic timeout to unstick the UI.
    yield await _sse_event("complete", {
        "status": "failed",
        "duration_ms": int(_SSE_TAIL_MAX_WAIT_S * 1000),
        "error": (
            f"SSE stream exceeded {int(_SSE_TAIL_MAX_WAIT_S)}s wait without "
            "terminal event — check install_runner subprocess state"
        ),
        "ts": _utc_iso(),
    })


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
    """Polling fallback for SSE — returns current state + log tail.

    Reads from disk (~/.nodeble-api/data/installs/<id>/state.json).
    """
    state = install_state.read(install_id)
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
    state = install_state.read(install_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"install_id not found: {install_id}")

    lines = [
        f"[{e['ts']}] [{e['level'].upper()}] {e['message']}"
        for e in state.get("log_tail", [])
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


# ── Server logs (1 endpoint) ────────────────────────────────────────────────


# journalctl PRIORITY → our 3-level taxonomy.
# Per `man systemd.journal-fields`: 0=emerg, 1=alert, 2=crit, 3=err,
# 4=warning, 5=notice, 6=info, 7=debug. We collapse to error/warn/info.
_JOURNALCTL_PRIORITY_TO_LEVEL = {
    "0": "error", "1": "error", "2": "error", "3": "error",
    "4": "warn",
    "5": "info", "6": "info", "7": "info",
}

# Map our 3-level taxonomy to journalctl's `-p` filter (max priority).
# `-p info` shows priorities 0-6 (everything except debug); `-p warn`
# shows 0-4; `-p err` shows 0-3. This matches "show this severity AND
# above" semantics that frontend operators expect.
_LEVEL_TO_JOURNALCTL_PRIORITY = {
    "info": "info",
    "warn": "warning",
    "error": "err",
}


def _journalctl_record_to_log_line(record: dict) -> dict:
    """Map one journalctl `-o json` record dict to {ts, level, message}.

    journalctl json fields:
      - ``__REALTIME_TIMESTAMP``: microseconds since epoch (string)
      - ``MESSAGE``: log text
      - ``PRIORITY``: syslog priority "0"-"7"
    """
    ts_us = record.get("__REALTIME_TIMESTAMP")
    ts_iso: str | None = None
    if ts_us:
        try:
            ts_iso = datetime.fromtimestamp(
                int(ts_us) / 1_000_000, tz=timezone.utc,
            ).isoformat()
        except (ValueError, TypeError):
            ts_iso = None
    priority = record.get("PRIORITY", "6")
    level = _JOURNALCTL_PRIORITY_TO_LEVEL.get(str(priority), "info")
    message = record.get("MESSAGE", "")
    if isinstance(message, list):
        # journalctl renders bytes-typed messages as int arrays; coerce to str.
        try:
            message = bytes(message).decode("utf-8", errors="replace")
        except Exception:
            message = repr(message)
    return {"ts": ts_iso, "level": level, "message": message}


@router.get("/logs/api-server")
def get_api_server_logs(lines: int = 200, level: str | None = None) -> dict:
    """Recent api-server systemd journal lines (Phase A Week 3 — real wiring).

    Invokes ``journalctl --user -u <unit> -n <lines> -o json [-p <priority>]``
    and parses the JSON-stream output line-by-line. Each record is mapped to
    the same ``{ts, level, message}`` shape the Phase A Week 1 stub returned,
    so frontend doesn't need a migration.

    Failure modes:
      - ``journalctl`` binary missing on host (e.g. macOS dev box) → 200
        with empty lines + ``source="journalctl_unavailable"`` so UI can
        show "logs unavailable" state cleanly.
      - subprocess exit non-zero → 500 with stderr tail.
      - subprocess timeout (5s) → 504.
    """
    if lines < 1 or lines > 500:
        raise HTTPException(status_code=400, detail="lines must be 1-500")
    if level is not None and level not in ("info", "warn", "error"):
        raise HTTPException(status_code=400, detail="level must be info|warn|error")

    if shutil.which("journalctl") is None:
        return {
            "lines": [],
            "total_returned": 0,
            "source": "journalctl_unavailable",
        }

    args = [
        "journalctl", "--user", "-u", _API_SERVER_SYSTEMD_UNIT,
        "-n", str(lines), "-o", "json", "--no-pager",
    ]
    if level:
        args.extend(["-p", _LEVEL_TO_JOURNALCTL_PRIORITY[level]])

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="journalctl timed out (>5s)")
    except (subprocess.SubprocessError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"journalctl failed: {exc}")

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"journalctl exited {result.returncode}: {result.stderr[-200:].strip()}",
        )

    parsed_lines: list[dict] = []
    for raw in result.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            # Defensive: journalctl sometimes emits non-JSON on truncated
            # records. Reuse logs.parse_log_line for free-form fallback.
            parsed = logs_module.parse_log_line(raw)
            parsed_lines.append({
                "ts": parsed.get("ts"),
                "level": (parsed.get("level") or "info").lower(),
                "message": parsed.get("message") or raw,
            })
            continue
        if not isinstance(record, dict):
            continue
        parsed_lines.append(_journalctl_record_to_log_line(record))

    return {
        "lines": parsed_lines[-lines:],
        "total_returned": min(len(parsed_lines), lines),
        "source": "journalctl_user",
    }

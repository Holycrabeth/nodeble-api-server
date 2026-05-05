"""Install state persistence — survives api-server restart.

Phase A Week 2 per Phase 4.1 contract freeze §6 Q4 decision (asyncio +
JSON state) + Backend Director plan Task A.4.

Each install's lifecycle persists to:
  ~/.nodeble-api/data/installs/<install_id>/state.json   — current state snapshot
  ~/.nodeble-api/data/installs/<install_id>/events.jsonl — append-only event log
                                                            (SSE replay source)

State schema (state.json)
-------------------------
{
  install_id: str,
  strategy: str,
  status: "queued" | "running" | "success" | "failed" | "cancelled",
  started_at: str (ISO 8601 UTC),
  completed_at: str | None,
  current_step: str | None,
  steps_completed: [{step, status, duration_ms, ts}],
  log_tail: [{level, message, ts}],
  error: str | None,
  config_with_mode_dry_run_enforced: dict (diagnostic),
  operation: "install" | "update",
  target_version: str | None  (for update only)
}

Events (events.jsonl)
---------------------
One JSON event per line, append-only via fcntl.flock. Same schema as
SSE wire format — `event` key + payload. Used by SSE endpoint to replay
to late subscribers + survive reconnect.

Restart recovery
----------------
On api-server boot, read all install dirs. For installs with
status="running": mark them as "failed" with error="api-server restarted
mid-install" (real subprocess died with parent). state.json updated,
final 'complete' event appended.

Concurrency
-----------
- One install_id = one subprocess = one event writer at a time
- fcntl.flock on events.jsonl during append for cross-process safety
- state.json atomic write via tempfile + os.replace
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_install_dir(install_id: str, home: Path | None = None) -> Path:
    """Path.home() resolved lazily so test monkeypatch works correctly."""
    base_home = home if home is not None else Path.home()
    return base_home / ".nodeble-api" / "data" / "installs" / install_id


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic JSON write to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".state_", suffix=".json.tmp",
    )
    try:
        os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def create(
    *,
    install_id: str,
    strategy: str,
    config: dict,
    operation: str = "install",
    target_version: str | None = None,
    home: Path | None = None,
) -> dict:
    """Create new install state on disk. Returns the state dict.

    Idempotent — if install_id dir exists, returns existing state without
    overwriting (matches POST /install idempotency contract).
    """
    install_dir = _resolve_install_dir(install_id, home)
    state_path = install_dir / "state.json"

    if state_path.exists():
        return read(install_id, home=home)

    started_at = _utc_iso()
    state = {
        "install_id": install_id,
        "strategy": strategy,
        "status": "queued",
        "started_at": started_at,
        "completed_at": None,
        "current_step": "Validating config",
        "steps_completed": [],
        "log_tail": [
            {"level": "info", "message": f"Install {install_id} queued for {strategy}",
             "ts": started_at},
        ],
        "error": None,
        "config_with_mode_dry_run_enforced": config,
        "operation": operation,
        "target_version": target_version,
    }

    install_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(state_path, state)
    # touch events.jsonl
    (install_dir / "events.jsonl").touch()

    return state


def read(install_id: str, home: Path | None = None) -> dict | None:
    """Read state.json for install_id. Returns None if not found."""
    state_path = _resolve_install_dir(install_id, home) / "state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def update_state(
    install_id: str,
    *,
    status: str | None = None,
    current_step: str | None = None,
    steps_completed_append: dict | None = None,
    log_tail_append: dict | None = None,
    completed_at: str | None = None,
    error: str | None = None,
    result_metadata_merge: dict | None = None,
    home: Path | None = None,
) -> dict | None:
    """Patch state.json for install_id.

    Reads current state, applies updates, atomic-writes back.
    Returns updated state, or None if install_id not found.

    Phase A Week 3 (Path C 5/5): ``result_metadata_merge`` accepts a dict
    of ``RESULT_<KEY>: <value>`` pairs collected from deploy.sh stdout per
    `~/projects/cto/reviews/2026-05-05-deploy-sh-non-interactive-contract.md`
    §4.5. Shallow-merges into ``state["result_metadata"]`` (creates the key
    if absent — backward-compat with state.json files written before this
    field was added).
    """
    state = read(install_id, home=home)
    if state is None:
        return None

    if status is not None:
        state["status"] = status
    if current_step is not None:
        state["current_step"] = current_step
    if steps_completed_append is not None:
        state["steps_completed"].append(steps_completed_append)
    if log_tail_append is not None:
        state["log_tail"].append(log_tail_append)
        # cap log_tail at last 200 entries to bound disk usage
        state["log_tail"] = state["log_tail"][-200:]
    if completed_at is not None:
        state["completed_at"] = completed_at
    if error is not None:
        state["error"] = error
    if result_metadata_merge is not None:
        existing = state.get("result_metadata") or {}
        existing.update(result_metadata_merge)
        state["result_metadata"] = existing

    state_path = _resolve_install_dir(install_id, home) / "state.json"
    _atomic_write_json(state_path, state)
    return state


def append_event(
    install_id: str,
    *,
    event_type: str,
    payload: dict,
    home: Path | None = None,
) -> bool:
    """Append SSE event to events.jsonl with fcntl.flock.

    Returns True on success, False if install_id not found.
    """
    install_dir = _resolve_install_dir(install_id, home)
    events_path = install_dir / "events.jsonl"
    if not install_dir.exists():
        return False

    record = {"event": event_type, "data": payload}
    line = json.dumps(record, separators=(",", ":")) + "\n"

    with open(events_path, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return True


def replay_events(install_id: str, home: Path | None = None) -> Iterator[dict]:
    """Yield all persisted events for install_id (SSE replay source).

    Each yielded item: {event: str, data: dict}.
    """
    events_path = _resolve_install_dir(install_id, home) / "events.jsonl"
    if not events_path.exists():
        return
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # corrupt line, skip


def list_installs(home: Path | None = None) -> list[str]:
    """List all install_id directories."""
    base_home = home if home is not None else Path.home()
    base = base_home / ".nodeble-api" / "data" / "installs"
    if not base.exists():
        return []
    return sorted([d.name for d in base.iterdir() if d.is_dir()])


def cleanup_stale_running(home: Path | None = None) -> list[str]:
    """On boot, mark any 'running' or 'queued' installs as 'failed'.

    api-server restart kills any in-flight subprocess (no daemon process
    survives systemctl restart). Cleaning these up prevents stale state
    from confusing the SSE/status endpoints.

    Returns list of install_ids that were cleaned up.
    """
    cleaned = []
    for install_id in list_installs(home=home):
        state = read(install_id, home=home)
        if state is None:
            continue
        if state.get("status") in ACTIVE_STATUSES:
            now = _utc_iso()
            update_state(
                install_id,
                status="failed",
                completed_at=now,
                error="api-server restarted mid-install (subprocess lost)",
                log_tail_append={
                    "level": "warn",
                    "message": "api-server restarted; this install was in flight and is now failed",
                    "ts": now,
                },
                home=home,
            )
            append_event(
                install_id,
                event_type="complete",
                payload={
                    "status": "failed",
                    "duration_ms": 0,
                    "error": "api-server restarted mid-install",
                    "ts": now,
                },
                home=home,
            )
            cleaned.append(install_id)
    return cleaned

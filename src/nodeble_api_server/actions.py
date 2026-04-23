"""On-demand strategy actions (scan / manage / close), invoked from the
desktop app as a subprocess against the strategy's own `python -m <pkg>`
CLI — NOT through the config shim layer. These are imperative "run once
and tell me what happened" operations, distinct from the config-editing
path that M1.h/M2.a set up.

Why a separate module from `config_writer`:
- Config edits are tight 10s shim subprocesses that return a JSON line.
  They're frequent (every param tweak) and touch yaml only.
- Actions are long-running (5-30s for scan; manage can be longer), talk
  to the broker, and return free-form stdout/stderr we want to surface to
  the operator. The shim contract doesn't fit — the strategy CLI already
  prints human-readable output and we don't want to wrap that in JSON.

MVP scope (M3.a): dry_run scan only. Live scan and close are next; they
need the same subprocess plumbing plus a LIVE confirmation UX on the
desktop side. Putting the shared machinery here means those later phases
are thin route additions, not structural rework.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nodeble_api_server.state_reader import (
    STRATEGY_REGISTRY,
    clear_cache,
    strategy_venv_python,
)

_SERVER_TZ = ZoneInfo("America/New_York")

# Scan timeouts: a cold scan that has to hit the broker for quotes on a
# dozen expiries can take 15-25s on a bad day. 30s gives us headroom
# without making the UI wait forever if something is truly stuck.
DEFAULT_SCAN_TIMEOUT_SEC = 30.0

# How much stdout / stderr to surface back to the UI. The full log lives
# in the strategy's own log file; this is just enough for the operator
# to see "ok, it decided X" or "broker gave error Y" without waiting.
TAIL_LINES = 50


def _strategy_package(strategy_id: str) -> str | None:
    """Python module name we pass to `python -m`. IC is historically
    `nodeble` (no strategy suffix — it was first); the rest follow the
    `nodeble_<strategy>` convention.
    """
    if strategy_id == "ic":
        return "nodeble"
    if strategy_id in STRATEGY_REGISTRY:
        return f"nodeble_{strategy_id}"
    return None


@dataclass(frozen=True)
class ScanResult:
    """Outcome of a single scan invocation.

    `status` values:
      - "success"       — subprocess exit code 0
      - "exit_nonzero"  — subprocess exit code != 0 (e.g. scan found
                          errors but didn't crash outright)
      - "timeout"       — we SIGKILL'd it after the deadline
      - "spawn_error"   — couldn't start the subprocess at all
                          (venv missing, strategy unknown, etc.)
    """
    status: str
    exit_code: int | None
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    started_at: str
    completed_at: str
    error: str | None = None


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    """Last N lines of `text`, trimmed. Handles the common case where
    scan output is thousands of log lines — we only want the endgame."""
    if not text:
        return ""
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def run_strategy_scan(
    strategy_id: str,
    *,
    mode: str = "dry_run",
    force: bool = True,
    timeout_sec: float = DEFAULT_SCAN_TIMEOUT_SEC,
    home: Path | None = None,
) -> ScanResult:
    """Invoke `python -m <strategy-pkg> --mode scan [--dry-run] [--force]`
    against the strategy's own venv and return the outcome.

    `mode` is either "dry_run" (always passes --dry-run, safe) or "live"
    (omits --dry-run, subject to yaml mode). The route layer gates "live".

    `force` defaults to True because the operator pressing a button
    implies they want it to run NOW, not skip because of a cron gate
    (market-closed time, cooldown, etc.). The strategy itself is still
    responsible for refusing to place live trades when market is shut.
    """
    if mode not in ("dry_run", "live"):
        raise ValueError(f"mode must be 'dry_run' or 'live', got {mode!r}")

    pkg = _strategy_package(strategy_id)
    if pkg is None:
        return _spawn_error(
            strategy_id,
            f"unknown strategy: {strategy_id!r}",
        )

    venv = strategy_venv_python(strategy_id, home=home)
    if venv is None or not venv.exists():
        return _spawn_error(
            strategy_id,
            f"venv python not found: {venv}",
        )

    cmd: list[str] = [str(venv), "-m", pkg, "--mode", "scan"]
    if mode == "dry_run":
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")

    started = datetime.now(_SERVER_TZ)
    started_iso = started.isoformat()
    t0 = started.timestamp()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=_build_env(),
            # kill_on_failure isn't a subprocess.run flag — timeout raises
            # TimeoutExpired which we catch below and SIGKILL is the
            # default for the grace period in Python 3.12.
        )
    except subprocess.TimeoutExpired as e:
        # subprocess.run already killed the child by the time we get here
        # (3.12+ uses Popen.kill()). Surface what we did see before kill.
        duration_ms = int((datetime.now(_SERVER_TZ).timestamp() - t0) * 1000)
        completed_iso = datetime.now(_SERVER_TZ).isoformat()
        stdout_so_far = e.stdout or ""
        stderr_so_far = e.stderr or ""
        # stdout/stderr from TimeoutExpired may be bytes even with text=True
        # on some Python versions — guard:
        if isinstance(stdout_so_far, bytes):
            stdout_so_far = stdout_so_far.decode("utf-8", errors="replace")
        if isinstance(stderr_so_far, bytes):
            stderr_so_far = stderr_so_far.decode("utf-8", errors="replace")
        return ScanResult(
            status="timeout",
            exit_code=None,
            duration_ms=duration_ms,
            stdout_tail=_tail(stdout_so_far),
            stderr_tail=_tail(stderr_so_far),
            started_at=started_iso,
            completed_at=completed_iso,
            error=f"scan timed out after {timeout_sec}s",
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        return _spawn_error(strategy_id, f"spawn: {type(e).__name__}: {e}")

    completed = datetime.now(_SERVER_TZ)
    completed_iso = completed.isoformat()
    duration_ms = int((completed.timestamp() - t0) * 1000)

    # Drop the cache so the next GET /strategies or /positions sees any
    # state.json changes the scan just wrote. Cheap; the next load is 5s
    # TTL anyway, this just gives the UI an immediate refresh.
    clear_cache()

    status = "success" if proc.returncode == 0 else "exit_nonzero"
    return ScanResult(
        status=status,
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
        started_at=started_iso,
        completed_at=completed_iso,
        error=None if status == "success" else f"exit code {proc.returncode}",
    )


def _spawn_error(strategy_id: str, detail: str) -> ScanResult:
    """Return a ScanResult for pre-subprocess failures (unknown strategy,
    missing venv). duration is 0, timestamps are now. Kept as a helper
    so all early-exit paths produce the same shape."""
    now = datetime.now(_SERVER_TZ).isoformat()
    return ScanResult(
        status="spawn_error",
        exit_code=None,
        duration_ms=0,
        stdout_tail="",
        stderr_tail="",
        started_at=now,
        completed_at=now,
        error=detail,
    )


def _build_env() -> dict[str, str]:
    """Inherit the api-server's env. We do NOT inject PYTHONPATH here —
    the strategy's venv has the strategy as an installed package, so
    `python -m <pkg>` resolves via the venv's own site-packages. This is
    different from the shim path (config_writer.run_shim) where we need
    the api-server's own module importable in the strategy venv."""
    return dict(os.environ)

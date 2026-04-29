"""Phase A Week 3 — install_runner DRAFT (not yet wired to /install endpoint).

Subprocess invoker that runs `deploy.sh --non-interactive --config <json>`
for a strategy module, parses stdout/stderr line-by-line per the locked
deploy.sh contract (Wheel dev Phase C Q4 design lock), and emits SSE events
via install_state.append_event() so the existing /install/{id}/stream
endpoint replays them.

Status (2026-04-27)
-------------------
DRAFT. Not yet imported by routes/server.py. The mock generator in
`routes/server.py::_generate_install_events()` is still authoritative.
This module ships proper for production when Wheel dev's Phase C deploy.sh
refactor lands (~5/8 PR open, ~5/12 merge per Backend Director Phase C
calendar in `project_pending_dev_fixes.md`).

Wiring path on Week 3 ship
--------------------------
1. routes/server.py::post_install():
   - after install_state.create(), schedule a background task:
     asyncio.create_task(install_runner.run_install(install_id, strategy, config))
2. routes/server.py::_generate_install_events():
   - REMOVE the mock step generator (lines 295-380)
   - SSE generator becomes simply: replay events.jsonl + tail until 'complete'
3. install_runner runs subprocess in background; events.jsonl is the
   single source of truth that both SSE stream and /status endpoint read.

deploy.sh stdout contract (locked Wheel Q4)
-------------------------------------------
Lines from deploy.sh stdout/stderr fall into one of three shapes:

| Prefix | Meaning | Maps to SSE event |
|---|---|---|
| `STEP: <step name>` | Step start | event=step, status=in_progress |
| `STATUS: ok [<msg>]` | Step succeeded | event=step, status=ok, message? |
| `STATUS: fail <msg>` | Step failed | event=step, status=failed + complete(failed) |
| (bare line) | log output | event=log, level=info |
| (line on stderr) | log output | event=log, level=warn |

Truncation: STATUS messages capped at 200 chars + " (see logs)" suffix per
Wheel dev Q4 lock — that's deploy.sh's responsibility, not ours, but our
parser must NOT crash on long lines.

Failure modes
-------------
- subprocess exits non-zero with no STATUS: fail → emit synthetic
  complete(failed) with exit code in error field
- subprocess hangs (no stdout for >config budget) → SIGTERM, emit
  complete(failed) with timeout error
- bad UTF-8 in stdout → decode with errors='replace', emit log line
- stdout EOF before complete → emit synthetic complete(failed)

Concurrency
-----------
- One install_id = one subprocess (idempotency-by-create in install_state)
- Multiple installs run concurrently as separate asyncio tasks
- events.jsonl per-install dir uses fcntl.flock (already in install_state)
- No global lock — each install is independent

Production budget
-----------------
- Default total budget 600s (10min) for full Wheel install (Phase 4.1
  contract §2.3 "~2-3 min total"; 600s is generous for first-time
  pip install on slow link)
- Per-step idle timeout 120s (no stdout) → SIGTERM
- Override via config["budget_ms"] from POST /install request body
"""
from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nodeble_api_server import install_state


_DEFAULT_TOTAL_BUDGET_MS = 600_000  # 10 min
_DEFAULT_IDLE_TIMEOUT_S = 120        # SIGTERM if no stdout for 2 min

# Parser regex — anchored to BOL. Whitespace tolerant.
_STEP_RE = re.compile(r"^STEP:\s*(.+?)\s*$")
_STATUS_OK_RE = re.compile(r"^STATUS:\s*ok(?:\s+(.+))?\s*$")
_STATUS_FAIL_RE = re.compile(r"^STATUS:\s*fail\s+(.+?)\s*$")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ParsedLine:
    """Output of _parse_line — describes what kind of SSE event to emit.

    `event_type`: 'step' | 'log' (never 'complete' — that's emitted by
                  the runner on subprocess exit, not from a stdout line)
    `payload`:    dict matching Phase 4.1 contract §2.2 schema
                  (sans `ts` — caller adds it)
    `is_terminal`: True if this is STATUS: fail (caller emits complete)
    """

    event_type: str
    payload: dict
    is_terminal: bool = False


def _parse_line(line: str, is_stderr: bool = False) -> ParsedLine:
    """Parse one stdout/stderr line into an SSE event payload.

    Pure function — no side effects. Tested standalone.
    """
    stripped = line.rstrip("\r\n")

    m = _STEP_RE.match(stripped)
    if m:
        return ParsedLine(
            event_type="step",
            payload={"step": m.group(1), "status": "in_progress"},
        )

    m = _STATUS_OK_RE.match(stripped)
    if m:
        msg = m.group(1)
        payload = {"status": "ok"}
        if msg:
            payload["message"] = msg
        return ParsedLine(event_type="step", payload=payload)

    m = _STATUS_FAIL_RE.match(stripped)
    if m:
        return ParsedLine(
            event_type="step",
            payload={"status": "failed", "error": m.group(1)},
            is_terminal=True,
        )

    # Bare line → log event. stderr → warn, stdout → info.
    return ParsedLine(
        event_type="log",
        payload={
            "level": "warn" if is_stderr else "info",
            "message": stripped,
        },
    )


async def _drain_stream(
    stream: asyncio.StreamReader,
    install_id: str,
    is_stderr: bool,
    current_step: dict,
    home: Optional[Path] = None,
) -> Optional[dict]:
    """Read lines from `stream`, parse, append events to events.jsonl.

    `current_step` is a 1-element dict used to track which step is open
    (so STATUS: ok can attach the right step name + duration_ms).

    Returns: terminal failure event (STATUS: fail payload) if encountered,
             else None.
    """
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            # Pathological 1MB+ line or stream cut — log and continue
            install_state.append_event(
                install_id,
                event_type="log",
                payload={
                    "level": "warn",
                    "message": "stream read error — line skipped",
                    "ts": _utc_iso(),
                },
                home=home,
            )
            continue

        if not raw:
            return None  # EOF

        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            line = repr(raw)

        parsed = _parse_line(line, is_stderr=is_stderr)
        ts = _utc_iso()
        payload = {**parsed.payload, "ts": ts}

        # Track step name + start time for STATUS: ok duration calculation
        if parsed.event_type == "step" and parsed.payload.get("status") == "in_progress":
            current_step["name"] = parsed.payload["step"]
            current_step["started_at"] = asyncio.get_event_loop().time()
            install_state.update_state(
                install_id,
                current_step=parsed.payload["step"],
                home=home,
            )
        elif parsed.event_type == "step" and parsed.payload.get("status") == "ok":
            # Attach step name from current_step + compute duration
            if current_step.get("name"):
                payload["step"] = current_step["name"]
            if current_step.get("started_at"):
                payload["duration_ms"] = int(
                    (asyncio.get_event_loop().time() - current_step["started_at"]) * 1000
                )
            install_state.update_state(
                install_id,
                steps_completed_append={
                    "step": payload.get("step"),
                    "status": "ok",
                    "duration_ms": payload.get("duration_ms"),
                    "ts": ts,
                },
                home=home,
            )
        elif parsed.event_type == "log":
            # Append a log-tail entry (capped at 200 internally by install_state)
            install_state.update_state(
                install_id,
                log_tail_append={
                    "level": parsed.payload["level"],
                    "message": parsed.payload["message"],
                    "ts": ts,
                },
                home=home,
            )

        install_state.append_event(
            install_id,
            event_type=parsed.event_type,
            payload=payload,
            home=home,
        )

        if parsed.is_terminal:
            # Attach step name to the failure event too
            if current_step.get("name"):
                payload["step"] = current_step["name"]
            return payload


async def run_install(
    *,
    install_id: str,
    cmd: list[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    total_budget_ms: int = _DEFAULT_TOTAL_BUDGET_MS,
    home: Optional[Path] = None,
) -> dict:
    """Spawn deploy.sh subprocess for `install_id`, parse stdout, emit SSE events.

    This is the Phase A Week 3 entry point. Called from
    `routes/server.py::post_install()` as a background asyncio task.

    Parameters
    ----------
    install_id : str
        Must already exist in install_state (caller does install_state.create()).
    cmd : list[str]
        Argv for subprocess. Typically:
        ["bash", str(deploy_sh_path), "--non-interactive", "--config", str(cfg_json)]
    cwd : Path | None
        Working directory for subprocess.
    env : dict | None
        Environment variables (subprocess inherits parent if None).
    total_budget_ms : int
        Hard wall-clock cap. SIGTERM at budget; SIGKILL 5s later.
    home : Path | None
        Override $HOME for tests (test isolation per install_state pattern).

    Returns
    -------
    dict : The 'complete' event payload that was emitted (status: success | failed).

    Side effects
    ------------
    - Spawns subprocess
    - Appends events to events.jsonl (one per stdout line, plus terminal complete)
    - Updates state.json (current_step, steps_completed, log_tail, status,
                         completed_at, error)
    """
    # Mark running
    install_state.update_state(install_id, status="running", home=home)

    started_t = asyncio.get_event_loop().time()
    current_step: dict = {}  # mutable shared state for STATUS: ok lookup

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

    # Drain stdout + stderr concurrently
    stdout_task = asyncio.create_task(
        _drain_stream(proc.stdout, install_id, is_stderr=False,
                      current_step=current_step, home=home),
    )
    stderr_task = asyncio.create_task(
        _drain_stream(proc.stderr, install_id, is_stderr=True,
                      current_step=current_step, home=home),
    )

    timeout_s = total_budget_ms / 1000.0
    timed_out = False
    terminal_payload: Optional[dict] = None

    try:
        results = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=timeout_s,
        )
        # If either stream produced a STATUS: fail terminal payload, capture it
        for r in results:
            if r is not None:
                terminal_payload = r
    except asyncio.TimeoutError:
        timed_out = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        # Cancel still-running stream drains
        for t in (stdout_task, stderr_task):
            if not t.done():
                t.cancel()

    # Wait for subprocess exit (it may have completed already; this is no-op)
    rc = await proc.wait()

    completed_at = _utc_iso()
    elapsed_ms = int((asyncio.get_event_loop().time() - started_t) * 1000)

    # Decide final status
    if timed_out:
        complete_event = {
            "status": "failed",
            "duration_ms": elapsed_ms,
            "error": f"install exceeded budget {total_budget_ms}ms — SIGTERM",
            "ts": completed_at,
        }
    elif terminal_payload is not None:
        # STATUS: fail line was the authoritative signal
        complete_event = {
            "status": "failed",
            "duration_ms": elapsed_ms,
            "error": terminal_payload.get("error", "step failed"),
            "ts": completed_at,
        }
    elif rc != 0:
        complete_event = {
            "status": "failed",
            "duration_ms": elapsed_ms,
            "error": f"deploy.sh exited with code {rc}",
            "ts": completed_at,
        }
    else:
        complete_event = {
            "status": "success",
            "duration_ms": elapsed_ms,
            "ts": completed_at,
        }

    install_state.update_state(
        install_id,
        status=complete_event["status"],
        completed_at=completed_at,
        error=complete_event.get("error"),
        home=home,
    )
    install_state.append_event(
        install_id,
        event_type="complete",
        payload=complete_event,
        home=home,
    )
    return complete_event


# ── Helpers for routes/server.py wiring (Week 3 ship) ──────────────────────


def build_deploy_cmd(
    *,
    strategy: str,
    config_json_path: Path,
    deploy_root: Path,
) -> list[str]:
    """Construct the argv for `bash <module>/deploy/deploy.sh --non-interactive --config X.json`.

    `deploy_root` is the per-strategy module checkout path (e.g.
    /home/mayongtao/projects/nodeble-wheel for strategy=wheel).
    """
    deploy_sh = deploy_root / "deploy" / "deploy.sh"
    return [
        "bash",
        str(deploy_sh),
        "--non-interactive",
        "--config",
        str(config_json_path),
    ]

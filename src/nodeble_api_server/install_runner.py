"""Phase A Week 3 — install_runner (Path C 5/5 contract — WIRED).

Subprocess invoker that runs ``deploy.sh --non-interactive --config <json>``
for a strategy module, parses stdout/stderr line-by-line per the
**5/5 deploy.sh non-interactive contract** (CTO ratify
`~/projects/cto/reviews/2026-05-05-deploy-sh-non-interactive-contract.md`),
and emits SSE events via ``install_state.append_event()`` so the
``/install/{id}/stream`` endpoint replays them.

Status (2026-05-05 SGT)
-----------------------
WIRED. ``routes/server.py::post_install()`` schedules
``asyncio.create_task(run_install(...))`` immediately after
``install_state.create()``. The mock 10-step ``_generate_install_events``
generator is replaced with a real replay-events.jsonl + tail SSE
generator so subscribers see live subprocess output.

5/5 contract DRIFT NOTE
-----------------------
This module was originally drafted (2026-04-27) against the older
"Wheel Q4 lock" stdout shape (per-step ``STATUS: ok`` / ``STATUS: fail``).
The 2026-05-05 ratified shared 4-module contract supersedes that and uses
a different shape (per-step start + ✓/✗ end markers + single final
``STATUS:`` terminal + ``RESULT_*`` metadata). This file matches the new
contract; the older parser regexes are gone.

deploy.sh stdout contract (5/5 lock)
------------------------------------
Lines from deploy.sh stdout/stderr fall into one of these shapes:

| Line shape                              | Meaning                          | SSE event |
|-----------------------------------------|----------------------------------|-----------|
| ``STEP: <id>``                          | Step start                        | ``step`` status=in_progress |
| ``STEP: <id> ✓``                        | Step success                      | ``step`` status=ok |
| ``STEP: <id> ✗ <one-line summary>``     | Step failure                      | ``step`` status=failed |
| ``STATUS: success``                     | Install completed cleanly         | ``complete`` status=success |
| ``STATUS: already_installed``           | Idempotent re-run, no-op          | ``complete`` status=already_installed |
| ``STATUS: failure: <reason>``           | Install failed (terminal)         | ``complete`` status=failed |
| ``RESULT_<KEY>: <value>``               | Structured metadata (collected)   | merged into state.result_metadata |
| (bare line stdout)                      | log output                        | ``log`` level=info |
| (bare line stderr)                      | log output                        | ``log`` level=warn |

Step IDs are kebab-case, lowercase. Step names cannot themselves contain
" ✓" or " ✗" so the marker detection is unambiguous (per spec §4.3).

Failure modes
-------------
- subprocess exits non-zero with no terminal ``STATUS:`` line → emit
  synthetic complete(failed) with exit code in error field
- subprocess exceeds total budget → SIGTERM, SIGKILL after 5s, emit
  complete(failed) with timeout error
- bad UTF-8 in stdout → decode with errors='replace', emit log line
- stdout EOF before terminal STATUS → emit synthetic complete(failed)

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
- Override via ``total_budget_ms`` arg from ``run_install()`` caller
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nodeble_api_server import install_state


_DEFAULT_TOTAL_BUDGET_MS = 600_000  # 10 min

# 5/5 contract parsers — anchored to BOL.
# RESULT lines: "RESULT_<KEY>: <value>" where KEY is uppercase + underscore.
_RESULT_RE = re.compile(r"^RESULT_([A-Z][A-Z0-9_]*):\s*(.*)$")
# Terminal STATUS lines.
_STATUS_SUCCESS_RE = re.compile(r"^STATUS:\s*success\s*$")
_STATUS_ALREADY_RE = re.compile(r"^STATUS:\s*already_installed\s*$")
_STATUS_FAILURE_RE = re.compile(r"^STATUS:\s*failure:\s*(.+?)\s*$")
# STEP line — body parsing handled in code (✓/✗ marker detection).
_STEP_PREFIX = "STEP:"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ParsedLine:
    """Output of :func:`_parse_line` — describes what to emit downstream.

    Attributes
    ----------
    event_type
        ``'step'`` (start/ok/failed) | ``'log'`` (bare line) |
        ``'result'`` (RESULT_* metadata) | ``'status'`` (terminal).
        Never ``'complete'`` directly — caller emits that on subprocess exit.
    payload
        Dict matching SSE event schema (sans ``ts`` — caller adds it).
    is_terminal
        True if this is a final ``STATUS:`` line (success | already_installed
        | failure). Caller emits a ``complete`` event.
    """

    event_type: str
    payload: dict = field(default_factory=dict)
    is_terminal: bool = False


def _parse_line(line: str, is_stderr: bool = False) -> ParsedLine:
    """Parse one stdout/stderr line into an SSE event payload.

    Pure function — no side effects. Tested standalone.

    Parsing precedence (most specific → least):
      1. Terminal ``STATUS:`` (success / already_installed / failure: …)
      2. ``RESULT_<KEY>: <value>``
      3. ``STEP:`` + body (further classified by trailing ✓ / ✗ marker)
      4. Bare line → ``log`` event (info if stdout, warn if stderr)
    """
    stripped = line.rstrip("\r\n")

    # 1. Terminal STATUS lines (highest specificity)
    if _STATUS_SUCCESS_RE.match(stripped):
        return ParsedLine(
            event_type="status",
            payload={"status": "success"},
            is_terminal=True,
        )
    if _STATUS_ALREADY_RE.match(stripped):
        return ParsedLine(
            event_type="status",
            payload={"status": "already_installed"},
            is_terminal=True,
        )
    m = _STATUS_FAILURE_RE.match(stripped)
    if m:
        return ParsedLine(
            event_type="status",
            payload={"status": "failed", "error": m.group(1)},
            is_terminal=True,
        )

    # 2. RESULT_<KEY>: <value>
    m = _RESULT_RE.match(stripped)
    if m:
        return ParsedLine(
            event_type="result",
            payload={"key": m.group(1), "value": m.group(2)},
        )

    # 3. STEP: ... — classify by trailing marker
    if stripped.startswith(_STEP_PREFIX):
        body = stripped[len(_STEP_PREFIX):].strip()
        # Step success: "<id> ✓" — body ends with " ✓" (or just "✓" if no name)
        if body.endswith(" ✓"):
            step_name = body[:-2].rstrip()
            return ParsedLine(
                event_type="step",
                payload={"step": step_name, "status": "ok"},
            )
        if body == "✓":
            return ParsedLine(
                event_type="step",
                payload={"step": "", "status": "ok"},
            )
        # Step failure: "<id> ✗ <summary>" — split on first " ✗ " or " ✗"
        if " ✗" in body:
            idx = body.index(" ✗")
            step_name = body[:idx].rstrip()
            after = body[idx + 2:].strip()  # may be "" if no summary
            return ParsedLine(
                event_type="step",
                payload={
                    "step": step_name,
                    "status": "failed",
                    "error": after if after else "step failed (no summary)",
                },
            )
        # Step start: "STEP: <id>" with no marker
        return ParsedLine(
            event_type="step",
            payload={"step": body, "status": "in_progress"},
        )

    # 4. Bare line → log
    return ParsedLine(
        event_type="log",
        payload={
            "level": "warn" if is_stderr else "info",
            "message": stripped,
        },
    )


@dataclass
class _RunnerCtx:
    """Mutable per-install state shared between drain coroutines."""

    install_id: str
    home: Optional[Path] = None
    # Step name + start monotonic ts for duration_ms calculation on step end.
    current_step_name: Optional[str] = None
    current_step_started_at: Optional[float] = None
    # Set when a terminal STATUS line is seen on either stream.
    terminal_payload: Optional[dict] = None
    # Collected RESULT_* metadata; flushed into state.result_metadata once.
    result_metadata: dict = field(default_factory=dict)


async def _drain_stream(
    stream: asyncio.StreamReader,
    ctx: _RunnerCtx,
    is_stderr: bool,
) -> None:
    """Read lines from ``stream``, parse, append events to events.jsonl + state.

    On terminal STATUS line, sets ``ctx.terminal_payload``. Caller checks
    after both streams drain.
    """
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            install_state.append_event(
                ctx.install_id,
                event_type="log",
                payload={
                    "level": "warn",
                    "message": "stream read error — line skipped",
                    "ts": _utc_iso(),
                },
                home=ctx.home,
            )
            continue

        if not raw:
            return  # EOF

        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            line = repr(raw)

        parsed = _parse_line(line, is_stderr=is_stderr)
        ts = _utc_iso()
        payload = {**parsed.payload, "ts": ts}

        if parsed.event_type == "step":
            status = parsed.payload.get("status")
            step_name = parsed.payload.get("step")

            if status == "in_progress":
                ctx.current_step_name = step_name
                ctx.current_step_started_at = asyncio.get_event_loop().time()
                install_state.update_state(
                    ctx.install_id,
                    current_step=step_name,
                    home=ctx.home,
                )
            elif status == "ok":
                # Compute duration if step start was tracked.
                duration_ms = None
                if (
                    ctx.current_step_name == step_name
                    and ctx.current_step_started_at is not None
                ):
                    duration_ms = int(
                        (asyncio.get_event_loop().time() - ctx.current_step_started_at)
                        * 1000
                    )
                if duration_ms is not None:
                    payload["duration_ms"] = duration_ms
                install_state.update_state(
                    ctx.install_id,
                    steps_completed_append={
                        "step": step_name,
                        "status": "ok",
                        "duration_ms": duration_ms,
                        "ts": ts,
                    },
                    home=ctx.home,
                )
            elif status == "failed":
                duration_ms = None
                if (
                    ctx.current_step_name == step_name
                    and ctx.current_step_started_at is not None
                ):
                    duration_ms = int(
                        (asyncio.get_event_loop().time() - ctx.current_step_started_at)
                        * 1000
                    )
                if duration_ms is not None:
                    payload["duration_ms"] = duration_ms
                install_state.update_state(
                    ctx.install_id,
                    steps_completed_append={
                        "step": step_name,
                        "status": "failed",
                        "duration_ms": duration_ms,
                        "ts": ts,
                        "error": parsed.payload.get("error"),
                    },
                    home=ctx.home,
                )

        elif parsed.event_type == "result":
            ctx.result_metadata[parsed.payload["key"]] = parsed.payload["value"]
            install_state.update_state(
                ctx.install_id,
                result_metadata_merge={
                    parsed.payload["key"]: parsed.payload["value"]
                },
                home=ctx.home,
            )

        elif parsed.event_type == "log":
            install_state.update_state(
                ctx.install_id,
                log_tail_append={
                    "level": parsed.payload["level"],
                    "message": parsed.payload["message"],
                    "ts": ts,
                },
                home=ctx.home,
            )

        elif parsed.event_type == "status":
            # Terminal — capture for run_install to read after streams drain.
            ctx.terminal_payload = {**payload}

        # All event types except "status" are mirrored to events.jsonl for SSE.
        # Status is captured by the caller and emitted as the "complete" event
        # after the subprocess exits, which keeps the SSE stream's terminal
        # event consistently named "complete" (not "status").
        if parsed.event_type != "status":
            install_state.append_event(
                ctx.install_id,
                event_type=parsed.event_type,
                payload=payload,
                home=ctx.home,
            )


async def run_install(
    *,
    install_id: str,
    cmd: list[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    total_budget_ms: int = _DEFAULT_TOTAL_BUDGET_MS,
    home: Optional[Path] = None,
) -> dict:
    """Spawn deploy.sh subprocess for ``install_id``, parse stdout, emit SSE events.

    Phase A Week 3 entry point. Called from ``routes/server.py::post_install()``
    as a background asyncio task.

    Parameters
    ----------
    install_id
        Must already exist in install_state (caller does ``install_state.create()``).
    cmd
        Argv for subprocess. Typically built by :func:`build_deploy_cmd`:
        ``["bash", str(deploy_sh_path), "--non-interactive", "--config", str(cfg)]``.
    cwd
        Working directory for subprocess.
    env
        Environment variables (subprocess inherits parent if None).
    total_budget_ms
        Hard wall-clock cap. SIGTERM at budget; SIGKILL 5s later.
    home
        Override ``$HOME`` for tests (test isolation per install_state pattern).

    Returns
    -------
    dict
        The 'complete' event payload that was emitted (status: success |
        already_installed | failed) plus optional ``result_metadata``
        snapshot of all collected RESULT_* keys.

    Side effects
    ------------
    - Spawns subprocess
    - Appends events to events.jsonl per stdout line + terminal complete
    - Updates state.json (current_step, steps_completed, log_tail,
      result_metadata, status, completed_at, error)
    """
    install_state.update_state(install_id, status="running", home=home)

    started_t = asyncio.get_event_loop().time()
    ctx = _RunnerCtx(install_id=install_id, home=home)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
    except (OSError, FileNotFoundError) as exc:
        # subprocess failed to start (e.g. bash missing or deploy.sh path bad)
        completed_at = _utc_iso()
        complete_event = {
            "status": "failed",
            "duration_ms": 0,
            "error": f"subprocess failed to start: {exc}",
            "ts": completed_at,
        }
        install_state.update_state(
            install_id,
            status="failed",
            completed_at=completed_at,
            error=complete_event["error"],
            home=home,
        )
        install_state.append_event(
            install_id,
            event_type="complete",
            payload=complete_event,
            home=home,
        )
        return complete_event

    stdout_task = asyncio.create_task(_drain_stream(proc.stdout, ctx, is_stderr=False))
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr, ctx, is_stderr=True))

    timeout_s = total_budget_ms / 1000.0
    timed_out = False

    try:
        await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        timed_out = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        for t in (stdout_task, stderr_task):
            if not t.done():
                t.cancel()

    rc = await proc.wait()
    completed_at = _utc_iso()
    elapsed_ms = int((asyncio.get_event_loop().time() - started_t) * 1000)

    # Decide final status — order of precedence:
    #   1. timeout (hard wall-clock cap exceeded)
    #   2. terminal STATUS line from deploy.sh stdout (authoritative)
    #   3. subprocess exit code (rc != 0 → fail; rc == 0 with no STATUS → fail)
    if timed_out:
        complete_event = {
            "status": "failed",
            "duration_ms": elapsed_ms,
            "error": f"install exceeded budget {total_budget_ms}ms — SIGTERM",
            "ts": completed_at,
        }
    elif ctx.terminal_payload is not None:
        terminal_status = ctx.terminal_payload.get("status")
        # Map "success"/"already_installed" to themselves; "failed" stays "failed".
        complete_event = {
            "status": terminal_status,
            "duration_ms": elapsed_ms,
            "ts": completed_at,
        }
        if terminal_status == "failed":
            complete_event["error"] = ctx.terminal_payload.get("error", "step failed")
    elif rc != 0:
        complete_event = {
            "status": "failed",
            "duration_ms": elapsed_ms,
            "error": f"deploy.sh exited with code {rc} (no terminal STATUS)",
            "ts": completed_at,
        }
    else:
        # Exit 0 with no terminal STATUS line — treat as failure (contract
        # violation: deploy.sh MUST emit STATUS:success when exiting 0).
        complete_event = {
            "status": "failed",
            "duration_ms": elapsed_ms,
            "error": "deploy.sh exited 0 without STATUS terminal line (contract violation)",
            "ts": completed_at,
        }

    if ctx.result_metadata:
        complete_event["result_metadata"] = dict(ctx.result_metadata)

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


# ── Helpers for routes/server.py wiring ────────────────────────────────────


def build_deploy_cmd(
    *,
    strategy: str,
    config_json_path: Path,
    deploy_root: Path,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """Construct argv for ``bash <module>/deploy/deploy.sh --non-interactive --config X.json``.

    ``deploy_root`` is the per-strategy module checkout path (e.g.
    ``/home/mayongtao/projects/nodeble-wheel`` for ``strategy=wheel``).
    ``extra_args`` are appended verbatim — used for ``--skip-telegram`` etc.
    """
    deploy_sh = deploy_root / "deploy" / "deploy.sh"
    cmd = [
        "bash",
        str(deploy_sh),
        "--non-interactive",
        "--config",
        str(config_json_path),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd

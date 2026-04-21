"""Subprocess wrapper for invoking per-strategy shims.

Each shim is a standalone Python script in
`nodeble_api_server.shims.*` that we run with the TARGET strategy's
venv interpreter so the shim can import the strategy's own modules
(Group A shims need this; Group B/C/D shims don't strictly but we use
the strategy venv uniformly for predictable import paths).

Contract:
- stdout: one JSON line `{"ok": bool, "old": any, "new": any, "error": str|null}`
- stderr: ignored unless shim crashed outright
- timeout: 10 seconds hard-killed with SIGKILL (not SIGTERM — hung
  subprocess is the dominant failure we're guarding against)
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class ShimResult:
    ok: bool
    old: Any
    new: Any
    error: str | None


def _shim_module(shim_name: str) -> str:
    """Map shim_name → dotted module path for python -m."""
    return f"nodeble_api_server.shims.{shim_name}"


def run_shim(
    venv_python: Path,
    shim_name: str,
    action: str,
    strategy_id: str,
    param_path: str,
    value: Any,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    api_server_src: Path | None = None,
) -> ShimResult:
    """Invoke a shim via the strategy's venv python.

    `api_server_src` is the directory containing the `nodeble_api_server`
    package — we add it to PYTHONPATH so the target venv can import our
    shim module (the target venv knows nothing about api-server).
    Defaults to the parent of this module's containing package.
    """
    if api_server_src is None:
        api_server_src = Path(__file__).resolve().parent.parent

    cmd = [
        str(venv_python),
        "-m",
        _shim_module(shim_name),
        action,
        strategy_id,
        param_path,
        json.dumps(value),
    ]
    env_pythonpath = str(api_server_src)

    import os as _os
    env = dict(_os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{env_pythonpath}:{existing}" if existing else env_pythonpath
    )

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_sec,
            text=True,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ShimResult(
            ok=False,
            old=None,
            new=None,
            error=f"shim timed out after {timeout_sec}s",
        )
    except FileNotFoundError as e:
        return ShimResult(ok=False, old=None, new=None, error=f"spawn: {e}")

    if proc.returncode != 0 and not proc.stdout.strip():
        # Shim crashed before emitting JSON — stderr has the traceback.
        stderr = proc.stderr.strip().splitlines()
        tail = stderr[-1] if stderr else f"exit {proc.returncode}"
        return ShimResult(ok=False, old=None, new=None, error=f"shim crashed: {tail}")

    # Parse the last JSON line (shim should emit exactly one, but startup
    # warnings from Python can precede it on some systems).
    stdout = proc.stdout.strip()
    if not stdout:
        return ShimResult(
            ok=False, old=None, new=None, error="shim produced no output"
        )
    last_line = stdout.splitlines()[-1]
    try:
        payload = json.loads(last_line)
    except json.JSONDecodeError as e:
        return ShimResult(
            ok=False,
            old=None,
            new=None,
            error=f"shim stdout not JSON: {e}: {last_line[:200]}",
        )

    return ShimResult(
        ok=bool(payload.get("ok")),
        old=payload.get("old"),
        new=payload.get("new"),
        error=payload.get("error"),
    )

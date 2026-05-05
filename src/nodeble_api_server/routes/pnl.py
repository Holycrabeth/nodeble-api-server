"""``/api/v1/pnl/*`` — surface for nodeble-pnl aggregator data.

Phase O.B.1 (2026-05-05): single endpoint ``GET /current_usage``.

Per Q1 lock + CTO 2026-05-04 post-verify pattern: subprocess to
``nodeble_pnl`` CLI, no Python code import. Mirrors the
``routes/orchestrator.py::_run_detect_installed_subprocess`` shape so
both subprocess wrappers degrade identically (timeout → 504, non-zero
→ 500 with stderr tail, non-JSON → 500).

Dual-mode (per L1 §1.5 amendment): nodeble-pnl is Mac-app multi-module
infrastructure. Git-clone single-bot users don't run api-server, so
this endpoint never fires for them — no impact.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from nodeble_api_server.auth import require_bearer_token

logger = logging.getLogger(__name__)


# Subprocess timeout — `compute_current_usage` reads 9 strategy state files
# locally; should complete in well under 1s. 30s caps the worst case
# (filesystem hiccup) without tying up a FastAPI worker indefinitely.
PNL_SUBPROCESS_TIMEOUT_SEC = 30


def _pnl_python(home: Path | None = None) -> Path:
    """Path to nodeble-pnl's venv python. Lazy ``Path.home()`` resolution
    so test ``monkeypatch.setattr(Path, 'home', ...)`` works."""
    base = home or Path.home()
    return base / "projects" / "nodeble-pnl" / ".venv" / "bin" / "python"


router = APIRouter(
    prefix="/api/v1/pnl",
    dependencies=[Depends(require_bearer_token)],
)


@router.get("/current_usage")
def get_current_usage() -> dict:
    """Return per-strategy current capital usage from nodeble-pnl.

    Subprocess to ``python -m nodeble_pnl --current-usage`` and
    passthrough the JSON. Shape (verified live 2026-05-05)::

        {
          "current_usage": {"ic": 767.0, "wheel": 67160.0, ...},
          "as_of": "2026-05-05T04:24:05Z"
        }

    All 9 strategy keys are present even at zero (per PnL Dev's design
    decision 2026-05-05 — multi-mode clean).
    """
    python_path = _pnl_python()
    try:
        result = subprocess.run(
            [str(python_path), "-m", "nodeble_pnl", "--current-usage"],
            capture_output=True, text=True,
            timeout=PNL_SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"pnl subprocess timed out (>{PNL_SUBPROCESS_TIMEOUT_SEC}s)",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"pnl subprocess failed to start: {exc}",
        )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"pnl subprocess exited {result.returncode}: "
                   f"{result.stderr[-500:].strip()}",
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"pnl subprocess produced non-JSON: {exc}",
        )

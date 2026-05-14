"""``/api/v1/pnl/*`` — surface for nodeble-pnl aggregator data.

Phase O.B.1 (2026-05-05): single endpoint ``GET /current_usage``.

Per Q1 lock + CTO 2026-05-04 post-verify pattern: subprocess to
``nodeble_pnl`` CLI, no Python code import. Mirrors the
``routes/orchestrator.py::_run_detect_installed_subprocess`` shape so
both subprocess wrappers degrade identically (timeout → 504, generic
non-zero → 500 with stderr tail, non-JSON → 500).

Special case (PnL Dev PR #2 ``431d9b8``, 协作总监 T-20260514-142516):
returncode == 2 is the REAL #17 per-strategy LIVE-mode gating signal —
PnL CLI refused to emit a falsified-zero JSON because at least one
strategy has LIVE positions without a ``capital_used_<strategy>``
formula. Surface as HTTP 503 with body
``{"error":"formula_not_implemented","strategies_affected":[...]}`` so
downstream (allocator / dashboard) can branch on the specific class of
service-unavailable rather than a generic 500.

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
from fastapi.responses import JSONResponse

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

    Failure modes:
      - timeout > PNL_SUBPROCESS_TIMEOUT_SEC → 504
      - exit 2 (REAL #17 gate) → 503 ``{"error":"formula_not_implemented",
        "strategies_affected": [strategy, ...]}``
      - any other non-zero exit → 500 with stderr tail
      - non-JSON stdout (exit 0) → 500
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

    # REAL #17 per-strategy LIVE-mode gating (PnL Dev PR #2 `431d9b8`,
    # 协作总监 T-20260514-142516): CLI exits 2 when one or more strategies
    # have LIVE positions but no per-strategy `capital_used_<strategy>`
    # formula implemented — the falsified-zero refusal gate. stdout
    # still carries the JSON diagnostic (current_usage + as_of +
    # formula_errors); we surface the strategy list as 503 so downstream
    # allocator / dashboard treats it as a transient-but-meaningful
    # service-unavailable rather than a generic 500.
    if result.returncode == 2:
        strategies_affected: list[str] = []
        try:
            payload = json.loads(result.stdout)
            for err in payload.get("formula_errors", []) or []:
                strat = err.get("strategy") if isinstance(err, dict) else None
                if strat:
                    strategies_affected.append(strat)
        except (json.JSONDecodeError, AttributeError, TypeError):
            # Defensive: gate fired but stdout malformed — still return 503
            # so the caller knows it's a formula-impl gap, not a generic
            # crash. Empty strategies_affected signals "PnL CLI gated but
            # diagnostic unparseable; check server logs for stderr".
            logger.warning(
                "pnl gate (exit 2) but stdout JSON unparseable: %r (stderr: %s)",
                result.stdout[:200],
                result.stderr[-200:].strip(),
            )
        else:
            logger.warning(
                "pnl gate (exit 2) — formula_not_implemented for: %s",
                strategies_affected or "<empty>",
            )
        return JSONResponse(
            status_code=503,
            content={
                "error": "formula_not_implemented",
                "strategies_affected": strategies_affected,
            },
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

"""Straddle shim (Group D) — whitelist defined by api-server since
straddle has no bot_helpers setter of its own.
"""
from __future__ import annotations

from pathlib import Path

from nodeble_api_server.shims._whitelist_shim import run_shim

_WHITELIST = {
    # Kill-switch knob — shim-writable, UI hides it (see strangle.py comment).
    "mode": {"type": "str", "choices": ["live", "dry_run"]},
    # Selection — ATM + DTE
    "selection.atm_max_distance_pct": {"type": "float", "min": 0.0, "max": 0.1},
    "selection.dte_min": {"type": "int", "min": 1, "max": 365},
    "selection.dte_max": {"type": "int", "min": 1, "max": 365},
    # Selection — gates
    "selection.min_open_interest": {"type": "int", "min": 0, "max": 10000},
    "selection.max_spread_pct": {"type": "float", "min": 0.01, "max": 0.5},
    "selection.min_credit_pct_of_underlying": {"type": "float", "min": 0.0001, "max": 0.1},
    "selection.min_vix": {"type": "float", "min": 0.0, "max": 100.0},
    "selection.fomc_blackout_days": {"type": "int", "min": 0, "max": 30},
}


if __name__ == "__main__":
    run_shim("straddle", Path.home() / ".nodeble-straddle", _WHITELIST)

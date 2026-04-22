"""Iron Butterfly shim (Group D) — no bot_helpers at all (file missing);
api-server owns the whitelist entirely. Body + wing delta targets are
the defining IronButterfly knobs, plus DTE + IV quality gates.
"""
from __future__ import annotations

from pathlib import Path

from nodeble_api_server.shims._whitelist_shim import run_shim

_WHITELIST = {
    # Kill-switch knob — shim-writable, UI hides it (see strangle.py comment).
    "mode": {"type": "str", "choices": ["live", "dry_run"]},
    # Body / wing delta ranges
    "selection.body_delta_min": {"type": "float", "min": 0.1, "max": 0.7},
    "selection.body_delta_max": {"type": "float", "min": 0.1, "max": 0.7},
    "selection.wing_delta_min": {"type": "float", "min": 0.01, "max": 0.3},
    "selection.wing_delta_max": {"type": "float", "min": 0.01, "max": 0.3},
    "selection.wing_delta_target": {"type": "float", "min": 0.01, "max": 0.3},
    # DTE window
    "selection.dte_min": {"type": "int", "min": 1, "max": 365},
    "selection.dte_max": {"type": "int", "min": 1, "max": 365},
    "selection.dte_ideal": {"type": "int", "min": 1, "max": 365},
    # Quality gates
    "selection.min_open_interest": {"type": "int", "min": 0, "max": 10000},
    "selection.max_spread_pct": {"type": "float", "min": 0.01, "max": 0.5},
    "selection.min_credit_pct_of_underlying": {"type": "float", "min": 0.0001, "max": 0.1},
    "selection.min_iv_rank": {"type": "float", "min": 0.0, "max": 1.0},
    "selection.max_abs_regime_score": {"type": "float", "min": 0.0, "max": 1.0},
    "selection.max_new_positions_per_run": {"type": "int", "min": 1, "max": 10},
}


if __name__ == "__main__":
    run_shim("ironbutterfly", Path.home() / ".nodeble-ironbutterfly", _WHITELIST)

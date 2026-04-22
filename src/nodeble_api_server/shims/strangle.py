"""Strangle shim — whitelist is strangle's own _SETTABLE_PARAMS (21 paths)
but we re-type them + add min/max because strangle's own set_strategy_param
is just a string coerce with no range validation and a non-atomic write."""
from __future__ import annotations

from pathlib import Path

from nodeble_api_server.shims._whitelist_shim import run_shim

_WHITELIST = {
    # Kill-switch knob — shim-writable so the /system/killswitch endpoint
    # can flip it; the /config/editable-paths route hides it from the UI's
    # generic ✎ editor so users only flip via the dedicated killswitch modal.
    "mode": {"type": "str", "choices": ["live", "dry_run"]},
    # Selection — deltas
    "selection.delta_target": {"type": "float", "min": 0.01, "max": 0.5},
    "selection.delta_min": {"type": "float", "min": 0.01, "max": 0.5},
    "selection.delta_max": {"type": "float", "min": 0.01, "max": 0.5},
    # Selection — DTE
    "selection.dte_min": {"type": "int", "min": 1, "max": 365},
    "selection.dte_max": {"type": "int", "min": 1, "max": 365},
    "selection.dte_ideal": {"type": "int", "min": 1, "max": 365},
    # Selection — liquidity / quality gates
    "selection.min_open_interest": {"type": "int", "min": 0, "max": 10000},
    "selection.max_spread_pct": {"type": "float", "min": 0.01, "max": 0.5},
    "selection.min_credit_pct_of_underlying": {"type": "float", "min": 0.0001, "max": 0.1},
    "selection.min_vix": {"type": "float", "min": 0.0, "max": 100.0},
    "selection.max_abs_regime_score": {"type": "float", "min": 0.0, "max": 1.0},
    "selection.max_concurrent_positions": {"type": "int", "min": 1, "max": 50},
    "selection.fomc_blackout_days": {"type": "int", "min": 0, "max": 30},
    # Management
    "management.profit_take_pct": {"type": "float", "min": 0.1, "max": 3.0},
    "management.stop_loss_credit_multiple": {"type": "float", "min": 1.0, "max": 10.0},
}


if __name__ == "__main__":
    run_shim("strangle", Path.home() / ".nodeble-strangle", _WHITELIST)

"""Calendar shim — whitelist reflects the 11 params in calendar's own
SETTABLE_PARAMS dict, but we own atomic write + proper range validation
(calendar's set_config_param skips ranges and uses non-atomic open('w'))."""
from __future__ import annotations

from pathlib import Path

from nodeble_api_server.shims._whitelist_shim import run_shim

_WHITELIST = {
    "mode": {"type": "str", "choices": ["live", "dry_run"]},
    "management.profit_take_pct": {"type": "float", "min": 0.1, "max": 5.0},
    "management.stop_loss_pct": {"type": "float", "min": 0.1, "max": 1.0},
    "management.time_stop_front_dte": {"type": "int", "min": 0, "max": 60},
    "selection.max_concurrent_positions": {"type": "int", "min": 1, "max": 50},
    "selection.max_new_positions_per_run": {"type": "int", "min": 1, "max": 10},
    "selection.max_abs_regime_score": {"type": "float", "min": 0.0, "max": 1.0},
    "selection.min_open_interest": {"type": "int", "min": 0, "max": 10000},
    "selection.max_spread_pct": {"type": "float", "min": 0.01, "max": 0.5},
    "selection.min_debit": {"type": "float", "min": 0.1, "max": 100.0},
    "selection.max_debit_pct_of_underlying": {"type": "float", "min": 0.001, "max": 0.5},
}


if __name__ == "__main__":
    run_shim("calendar", Path.home() / ".nodeble-calendar", _WHITELIST)

"""Collar shim (Group D) — Collar has no param API, so api-server owns
the whitelist. Collar is a hedging overlay so the editable params skew
toward delta targets + roll timing rather than credit thresholds.
"""
from __future__ import annotations

from pathlib import Path

from nodeble_api_server.shims._whitelist_shim import run_shim

_WHITELIST = {
    # Selection — PUT leg
    "selection.put_delta_target": {"type": "float", "min": 0.05, "max": 0.5},
    "selection.put_delta_min": {"type": "float", "min": 0.05, "max": 0.5},
    "selection.put_delta_max": {"type": "float", "min": 0.05, "max": 0.5},
    # Selection — CALL leg
    "selection.call_delta_target": {"type": "float", "min": 0.05, "max": 0.5},
    "selection.call_delta_min": {"type": "float", "min": 0.05, "max": 0.5},
    "selection.call_delta_max": {"type": "float", "min": 0.05, "max": 0.5},
    # Selection — DTE
    "selection.dte_min": {"type": "int", "min": 1, "max": 365},
    "selection.dte_max": {"type": "int", "min": 1, "max": 365},
    "selection.max_concurrent_positions": {"type": "int", "min": 1, "max": 50},
    # Management — roll timing
    "management.roll_alert_dte": {"type": "int", "min": 0, "max": 60},
    "management.roll_trigger_dte": {"type": "int", "min": 0, "max": 60},
}


if __name__ == "__main__":
    run_shim("collar", Path.home() / ".nodeble-collar", _WHITELIST)

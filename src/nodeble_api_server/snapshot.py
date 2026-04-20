"""Build full state snapshots for both HTTP responses and WS broadcasts.

`build_snapshot` is the single source of truth for what the desktop app sees
— one entry point ensures HTTP fallback and WS push are always in sync.
"""
from __future__ import annotations

import socket
import time

from nodeble_api_server import __version__
from nodeble_api_server.state_reader import (
    build_strategy_card,
    list_installed_strategies,
)

API_VERSION = "v1"
_START_TIME = time.monotonic()


def build_server_info() -> dict:
    return {
        "version": __version__,
        "api_version": API_VERSION,
        "hostname": socket.gethostname(),
        "uptime_sec": int(time.monotonic() - _START_TIME),
    }


def build_strategies_list() -> list[dict]:
    return [build_strategy_card(sid) for sid in list_installed_strategies()]


def build_snapshot() -> dict:
    """Full payload: strategies list + server_info."""
    return {
        "strategies": build_strategies_list(),
        "server_info": build_server_info(),
    }

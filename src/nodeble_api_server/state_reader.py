"""File system reader for the 9 strategy modules' state/config/allocation.

Pattern per module: ~/.<folder>/data/state.json + config/{strategy,risk}.yaml +
data/signal_state.json. Allocation is centralized at
~/.nodeble-orchestrator/data/allocation.json.

All reads are cached 5 seconds to make /api/v1/strategies (9-strategy fan-out)
cheap. Writes invalidate nothing (read-only module); TTL covers cron updates.

Timestamp formats across strategies are inconsistent (date-only / ISO with or
without tz / empty string). `normalize_timestamp` funnels everything into
ISO 8601 + America/New_York.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as time_cls, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

SERVER_TZ = ZoneInfo("America/New_York")

STRATEGY_REGISTRY: dict[str, dict[str, str]] = {
    # log_file is the primary strategy-logic log file under <folder>/logs/.
    # Survey on 2026-04-21 shows three format families across the 9 strategies:
    #   1. Python stdlib text (ic/wheel/pmcc/directionalspread)
    #      — "TS,ms MODULE LEVEL MSG" (no brackets around LEVEL)
    #   2. JSON lines (calendar/ironbutterfly/straddle/strangle)
    #      — {"ts", "lvl", "subsys", "msg", ...}
    #   3. Mixed / stderr dumps (collar)
    # straddle uses scanner.log (not bot.log) because bot.log is tiny Telegram-
    # polling output while scanner.log has real scan decisions.
    #
    # config_shim maps to a module in nodeble_api_server.shims.* — the shim
    # owns path resolution, validation, and atomic writes for that strategy.
    # Four families (see shims/ for details):
    #   group_a  — ic/wheel/pmcc/directionalspread (delegates to bot_helpers.validate_param + set_config_value)
    #   calendar — api-server owns whitelist (calendar's set_config_param is non-atomic)
    #   strangle — api-server owns whitelist (strangle's set_strategy_param is non-atomic + no range check)
    #   <name>   — straddle/collar/ironbutterfly each have a small per-strategy shim because they expose NO setter
    # repo_dir is where the strategy's venv and source live.
    "ic":                {"name": "Iron Condor",    "folder": ".nodeble",                   "log_file": "nodeble.log",                   "config_shim": "group_a",       "repo_dir": "projects/nodeble"},
    "wheel":             {"name": "Wheel",          "folder": ".nodeble-wheel",             "log_file": "nodeble-wheel.log",             "config_shim": "group_a",       "repo_dir": "projects/nodeble-wheel"},
    "pmcc":              {"name": "PMCC",           "folder": ".nodeble-pmcc",              "log_file": "nodeble-pmcc.log",              "config_shim": "group_a",       "repo_dir": "projects/nodeble-pmcc"},
    "calendar":          {"name": "Calendar",       "folder": ".nodeble-calendar",          "log_file": "bot.log",                       "config_shim": "calendar",      "repo_dir": "projects/nodeble-calendar"},
    "collar":            {"name": "Collar",         "folder": ".nodeble-collar",            "log_file": "bot.log",                       "config_shim": "collar",        "repo_dir": "projects/nodeble-collar"},
    "directionalspread": {"name": "Credit Spread",  "folder": ".nodeble-directionalspread", "allocation_key": "cs",                      "log_file": "nodeble-directionalspread.log", "config_shim": "group_a", "repo_dir": "projects/nodeble-directionalspread"},
    "ironbutterfly":     {"name": "Iron Butterfly", "folder": ".nodeble-ironbutterfly",     "log_file": "bot.log",                       "config_shim": "ironbutterfly", "repo_dir": "projects/nodeble-ironbutterfly"},
    "straddle":          {"name": "Straddle",       "folder": ".nodeble-straddle",          "log_file": "scanner.log",                   "config_shim": "straddle",      "repo_dir": "projects/nodeble-straddle"},
    "strangle":          {"name": "Strangle",       "folder": ".nodeble-strangle",          "log_file": "bot.log",                       "config_shim": "strangle",      "repo_dir": "projects/nodeble-strangle"},
}

ACTIVE_POSITION_STATUSES: frozenset[str] = frozenset({"open", "pending", "partial", "assigned"})

_CACHE_TTL = 5.0
_cache: dict[tuple, tuple[Any, float]] = {}


# ── Cache ───────────────────────────────────────────────────────────────────

def _cached(key: tuple, loader: Callable[[], Any]) -> Any:
    now = time.monotonic()
    entry = _cache.get(key)
    if entry is not None and now - entry[1] < _CACHE_TTL:
        return entry[0]
    value = loader()
    _cache[key] = (value, now)
    return value


def clear_cache() -> None:
    """Test helper — drop all cached reads."""
    _cache.clear()


# ── Safe file readers ──────────────────────────────────────────────────────

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _read_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except (yaml.YAMLError, OSError):
        return None


# ── Timestamp normalization ─────────────────────────────────────────────────

def normalize_timestamp(raw: Any) -> str | None:
    """Normalize a state.json timestamp to ISO 8601 + America/New_York.

    Accepted inputs:
    - ISO datetime with tz       → pass through (stays as-is)
    - ISO datetime without tz    → localize to ET
    - Date-only "YYYY-MM-DD"     → treat as 00:00 ET that day
    - None / non-string / empty  → None
    - Malformed                  → None
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None

    # ISO datetime (with or without tz)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SERVER_TZ)
        return dt.isoformat()
    except ValueError:
        pass

    # Date-only
    try:
        d = date.fromisoformat(s)
        dt = datetime.combine(d, time_cls.min).replace(tzinfo=SERVER_TZ)
        return dt.isoformat()
    except ValueError:
        pass

    return None


def _from_ts_epoch(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=SERVER_TZ).isoformat()


# ── Public readers ──────────────────────────────────────────────────────────

def list_installed_strategies(home: Path | None = None) -> list[str]:
    home = home or Path.home()
    return [
        sid
        for sid, meta in STRATEGY_REGISTRY.items()
        if (home / meta["folder"] / "data" / "state.json").exists()
    ]


def read_state(strategy_id: str, home: Path | None = None) -> dict | None:
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta:
        return None
    path = home / meta["folder"] / "data" / "state.json"
    return _cached(("state", strategy_id, str(path)), lambda: _read_json(path))


def read_config(strategy_id: str, home: Path | None = None) -> dict | None:
    """Merge strategy.yaml + risk.yaml. strategy.yaml missing → None."""
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta:
        return None
    strat_path = home / meta["folder"] / "config" / "strategy.yaml"
    risk_path = home / meta["folder"] / "config" / "risk.yaml"

    def loader() -> dict | None:
        strat = _read_yaml(strat_path)
        if strat is None:
            return None
        risk = _read_yaml(risk_path)
        merged = dict(strat)
        if risk:
            # risk.yaml top-level is "risk:" already, merge as-is
            for k, v in risk.items():
                merged[k] = v
        return merged

    return _cached(("config", strategy_id, str(strat_path)), loader)


def read_signal_timestamp(strategy_id: str, home: Path | None = None) -> str | None:
    """Read signal_state.json and return its generated_at field (normalized)."""
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta:
        return None
    path = home / meta["folder"] / "data" / "signal_state.json"
    data = _cached(("signal", strategy_id, str(path)), lambda: _read_json(path))
    if not data:
        return None
    return normalize_timestamp(data.get("generated_at"))


def read_allocation(home: Path | None = None) -> dict | None:
    home = home or Path.home()
    path = home / ".nodeble-orchestrator" / "data" / "allocation.json"
    return _cached(("allocation", str(path)), lambda: _read_json(path))


def latest_log_mtime(strategy_id: str, home: Path | None = None) -> str | None:
    """Return the newest *.log mtime under ~/<folder>/logs/, ISO + ET. None if no logs."""
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta:
        return None
    logs_dir = home / meta["folder"] / "logs"
    if not logs_dir.exists():
        return None
    latest: float | None = None
    for log in logs_dir.glob("*.log"):
        try:
            mtime = log.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    if latest is None:
        return None
    return _from_ts_epoch(latest)


def strategy_log_path(strategy_id: str, home: Path | None = None) -> Path | None:
    """Absolute path to the strategy's primary log file. Returns None if
    the strategy id isn't registered; returns a path even if the file
    doesn't yet exist (callers decide how to handle missing files)."""
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta or "log_file" not in meta:
        return None
    return home / meta["folder"] / "logs" / meta["log_file"]


def strategy_venv_python(strategy_id: str, home: Path | None = None) -> Path | None:
    """Absolute path to the strategy's venv python interpreter. Used by
    config_writer.run_shim to invoke the target strategy's bot_helpers
    with the correct dependencies."""
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta or "repo_dir" not in meta:
        return None
    return home / meta["repo_dir"] / ".venv" / "bin" / "python"


def strategy_config_shim(strategy_id: str) -> str | None:
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta:
        return None
    return meta.get("config_shim")


# ── Position helpers ────────────────────────────────────────────────────────

def positions_as_list(positions_raw: Any) -> list[dict]:
    """Normalize positions to list[dict] regardless of source shape (dict or list)."""
    if isinstance(positions_raw, dict):
        out = []
        for spread_id, pos in positions_raw.items():
            if isinstance(pos, dict):
                pos = {**pos}  # copy
                pos.setdefault("spread_id", spread_id)
                out.append(pos)
        return out
    if isinstance(positions_raw, list):
        return [p for p in positions_raw if isinstance(p, dict)]
    return []


def count_active_positions(positions_raw: Any) -> int:
    return sum(
        1
        for p in positions_as_list(positions_raw)
        if p.get("status") in ACTIVE_POSITION_STATUSES
    )


def sum_active_budget(positions_raw: Any) -> float:
    """Σ max_risk × contracts × 100 over active positions (same formula bot_helpers uses)."""
    total = 0.0
    for p in positions_as_list(positions_raw):
        if p.get("status") not in ACTIVE_POSITION_STATUSES:
            continue
        max_risk = p.get("max_risk") or 0
        contracts = p.get("contracts") or 1
        try:
            total += float(max_risk) * float(contracts) * 100
        except (TypeError, ValueError):
            continue
    return total


# ── Health ──────────────────────────────────────────────────────────────────

def build_strategy_card(strategy_id: str, home: Path | None = None) -> dict:
    """Assemble the StrategyCard dict for a single strategy.

    Used by GET /api/v1/strategies (HTTP) and the WS broadcaster. Health is
    computed from state/signal timestamps only — log mtime is a display-only
    fallback so stale strategies don't masquerade as healthy just because
    their log file was touched (e.g. by logrotate or a tail).
    """
    meta = STRATEGY_REGISTRY[strategy_id]
    state = read_state(strategy_id, home=home) or {}
    config = read_config(strategy_id, home=home) or {}
    allocation = read_allocation(home=home) or {}

    enabled = config.get("mode", "live") != "disabled"

    positions_raw = state.get("positions", {})
    open_positions = count_active_positions(positions_raw)
    budget_used = sum_active_budget(positions_raw)

    alloc_key = meta.get("allocation_key", strategy_id)
    alloc_entry = (allocation.get("strategies") or {}).get(alloc_key) or {}
    budget_max = (
        alloc_entry.get("max_buying_power")
        or (config.get("capital") or {}).get("budget")
        or 0
    )

    state_scan = normalize_timestamp(state.get("last_scan_date"))
    state_manage = normalize_timestamp(state.get("last_manage_date"))
    state_signal = read_signal_timestamp(strategy_id, home=home)
    health = compute_health(state_scan, state_manage, state_signal)

    log_mtime = None
    if not (state_scan and state_manage and state_signal):
        log_mtime = latest_log_mtime(strategy_id, home=home)
    last_scan_at = state_scan or log_mtime
    last_manage_at = state_manage or log_mtime
    last_signal_at = state_signal or log_mtime

    return {
        "id": strategy_id,
        "name": meta["name"],
        "enabled": enabled,
        "open_positions": open_positions,
        "budget_used": budget_used,
        "budget_max": budget_max,
        "last_signal_at": last_signal_at,
        "last_scan_at": last_scan_at,
        "last_manage_at": last_manage_at,
        "health": health,
        "version": None,
        "today_pnl": None,
        "cumulative_pnl_7d": None,
        "cumulative_pnl_30d": None,
        "circuit_breaker": None,
    }


def compute_health(
    last_scan_at: str | None,
    last_manage_at: str | None,
    last_signal_at: str | None,
    now: datetime | None = None,
) -> str:
    """Health tier:
    - critical: last_scan_at or last_manage_at missing/unparseable
    - warning:  any of the three is > 24h old
    - healthy:  otherwise
    """
    if not last_scan_at or not last_manage_at:
        return "critical"
    if now is None:
        now = datetime.now(SERVER_TZ)
    threshold_sec = 24 * 3600
    for ts in (last_scan_at, last_manage_at, last_signal_at):
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SERVER_TZ)
        age = (now - dt).total_seconds()
        if age > threshold_sec:
            return "warning"
    return "healthy"

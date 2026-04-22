"""Daily PnL snapshot writer.

Background task that writes one JSONL row per strategy per day to
`~/.nodeble-api/history/daily-pnl.jsonl`. Fires once at app boot
(seeding today's point so the chart isn't empty) and then every
ET 23:59 via the snapshot_loop in app.py.

Storage model:
- Append-only JSONL, no rotation — ~200 bytes × 9 strategies × 365 days
  = ~660 KB/year, fine for several years.
- Idempotent per (strategy, date) tuple: re-running on the same day
  is a no-op for strategies already recorded. Makes boot-seed +
  scheduled tick + accidental double-fire all safe.
- Forward-only. No backfill from state.json history — that data
  doesn't exist cleanly, and faking it would be worse than honest
  "data accumulates going forward."

Row schema:
  date, snapshot_at, strategy,
  realized_pnl_cumulative, open_positions_count,
  budget_used, budget_max
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nodeble_api_server.state_reader import (
    STRATEGY_REGISTRY,
    count_active_positions,
    read_allocation,
    read_config,
    read_state,
    sum_active_budget,
)

log = logging.getLogger(__name__)

SERVER_TZ = ZoneInfo("America/New_York")
SNAPSHOT_TIME = time(23, 59, 0)

_DEFAULT_SNAPSHOT_PATH = Path(
    "~/.nodeble-api/history/daily-pnl.jsonl"
).expanduser()

_DEFAULT_POSITIONS_SNAPSHOT_PATH = Path(
    "~/.nodeble-api/history/daily-positions.jsonl"
).expanduser()


def snapshot_path() -> Path:
    """Resolved at call time so tests can monkeypatch HOME."""
    return _DEFAULT_SNAPSHOT_PATH


def positions_snapshot_path() -> Path:
    """Resolved at call time so tests can monkeypatch HOME."""
    return _DEFAULT_POSITIONS_SNAPSHOT_PATH


def _next_snapshot_time(now: datetime) -> datetime:
    """Return the next ET 23:59:00 datetime strictly after `now`.

    Pure function — no I/O, no global time. Tests drive `now` directly.

    If `now` has no tzinfo we treat it as ET (simplifies test setup).
    If `now` is already past 23:59 today, the answer is 23:59 tomorrow.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=SERVER_TZ)
    else:
        now = now.astimezone(SERVER_TZ)
    today_23_59 = now.replace(
        hour=SNAPSHOT_TIME.hour,
        minute=SNAPSHOT_TIME.minute,
        second=0,
        microsecond=0,
    )
    if now < today_23_59:
        return today_23_59
    return today_23_59 + timedelta(days=1)


def _build_row(
    strategy_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Assemble one row from a strategy's current state. Missing state
    → null values but still a row, so the file documents "we tried on
    this day" rather than silently dropping the strategy."""
    meta = STRATEGY_REGISTRY.get(strategy_id, {})
    base: dict[str, Any] = {
        "date": now.strftime("%Y-%m-%d"),
        "snapshot_at": now.isoformat(),
        "strategy": strategy_id,
        "realized_pnl_cumulative": None,
        "open_positions_count": 0,
        "budget_used": 0,
        "budget_max": 0,
    }

    state = read_state(strategy_id)
    if not state:
        return base

    try:
        pnl = state.get("total_realized_pnl")
        base["realized_pnl_cumulative"] = (
            float(pnl) if pnl is not None else None
        )
    except (TypeError, ValueError):
        base["realized_pnl_cumulative"] = None

    positions_raw = state.get("positions", {}) or {}
    try:
        base["open_positions_count"] = count_active_positions(positions_raw)
        base["budget_used"] = sum_active_budget(positions_raw)
    except Exception:
        log.exception("positions aggregation failed for %s", strategy_id)

    # Budget max lookup mirrors build_strategy_card's logic.
    try:
        config = read_config(strategy_id) or {}
        allocation = read_allocation() or {}
        alloc_key = meta.get("allocation_key", strategy_id)
        alloc_entry = (allocation.get("strategies") or {}).get(alloc_key) or {}
        base["budget_max"] = (
            alloc_entry.get("max_buying_power")
            or (config.get("capital") or {}).get("budget")
            or 0
        )
    except Exception:
        log.exception("budget_max lookup failed for %s", strategy_id)

    return base


def _already_snapshotted_today(
    path: Path,
    date_str: str,
) -> set[str]:
    """Return the set of strategy ids already recorded for `date_str`
    in the JSONL. Allows the writer to skip them for idempotency."""
    if not path.exists():
        return set()
    done: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(obj, dict)
                    and obj.get("date") == date_str
                    and isinstance(obj.get("strategy"), str)
                ):
                    done.add(obj["strategy"])
    except OSError:
        log.exception("snapshot: could not scan existing entries at %s", path)
    return done


def take_daily_snapshot(
    now: datetime | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Write one JSONL row per strategy that doesn't already have one
    for today (ET). Returns the list of rows actually written — useful
    for logging and for tests."""
    if now is None:
        now = datetime.now(SERVER_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=SERVER_TZ)
    else:
        now = now.astimezone(SERVER_TZ)

    path = path or snapshot_path()
    date_str = now.strftime("%Y-%m-%d")
    already = _already_snapshotted_today(path, date_str)
    to_write: list[dict] = []
    for strategy_id in STRATEGY_REGISTRY.keys():
        if strategy_id in already:
            continue
        try:
            row = _build_row(strategy_id, now)
            to_write.append(row)
        except Exception:
            log.exception("snapshot row failed for %s", strategy_id)

    if not to_write:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in to_write:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return to_write


def _build_positions_row(
    strategy_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Assemble one row for the positions snapshot — raw passthrough of
    the state.json `positions` value. No normalization: the frontend
    already knows how to render heterogeneous position shapes, and
    keeping the snapshot close to source makes future migrations
    (e.g. replaying into a reconciler) easier."""
    row: dict[str, Any] = {
        "date": now.strftime("%Y-%m-%d"),
        "snapshot_at": now.isoformat(),
        "strategy": strategy_id,
        "positions": [],
    }

    state = read_state(strategy_id)
    if not state:
        return row

    raw_positions = state.get("positions")
    if raw_positions is None:
        return row

    # state_reader uses dict-of-records for some strategies (IC, PMCC)
    # and list-of-records for others. Normalize to a list so the wire
    # shape is uniform; individual record structure is preserved.
    if isinstance(raw_positions, dict):
        row["positions"] = [
            {**v, "_spread_id": k}
            if isinstance(v, dict) and "_spread_id" not in v
            else v
            for k, v in raw_positions.items()
            if isinstance(v, dict)
        ]
    elif isinstance(raw_positions, list):
        row["positions"] = [p for p in raw_positions if isinstance(p, dict)]
    # Other shapes → leave as empty list (row still written).

    return row


def take_daily_positions_snapshot(
    now: datetime | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Write one JSONL row per strategy to daily-positions.jsonl.

    Same idempotency contract as `take_daily_snapshot`: skips
    strategies that already have a row for today's date. A separate
    file so PnL vs positions concerns don't entangle in the reader
    paths + we can rotate / archive them on different schedules later.
    """
    if now is None:
        now = datetime.now(SERVER_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=SERVER_TZ)
    else:
        now = now.astimezone(SERVER_TZ)

    path = path or positions_snapshot_path()
    date_str = now.strftime("%Y-%m-%d")
    already = _already_snapshotted_today(path, date_str)
    to_write: list[dict] = []
    for strategy_id in STRATEGY_REGISTRY.keys():
        if strategy_id in already:
            continue
        try:
            row = _build_positions_row(strategy_id, now)
            to_write.append(row)
        except Exception:
            log.exception("positions snapshot row failed for %s", strategy_id)

    if not to_write:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in to_write:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return to_write

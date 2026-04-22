"""Reader for `~/.nodeble-api/history/daily-positions.jsonl`.

Paired with `snapshot_writer.take_daily_positions_snapshot`. Companion
to `history_reader.py` (which reads the PnL jsonl) but deliberately a
separate module so the two concerns stay decoupled — the position
snapshot wire shape is larger and may evolve independently.

Two public functions:
- `read_available_dates(path, strategy, days)` — dates with at least
  one row for the strategy, newest-first, capped to `days` back.
- `read_positions_at_date(path, strategy, target_date)` — the full
  row for one (strategy, date) pair, or None.

Defensive reads: malformed JSON / non-dict entries / missing fields
are silently skipped. These are historical telemetry, not legal
evidence — we never mutate the file.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any


def _load_rows_for_strategy(path: Path, strategy: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
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
                if not isinstance(obj, dict):
                    continue
                if obj.get("strategy") != strategy:
                    continue
                date_str = obj.get("date")
                if not isinstance(date_str, str):
                    continue
                out.append(obj)
    except OSError:
        return []
    return out


def read_available_dates(
    path: Path,
    strategy: str,
    days: int = 90,
) -> list[str]:
    """Return the set of dates for which `strategy` has a snapshot,
    newest-first. Deduplicated (writer is idempotent but defensive anyway).
    Capped to the last `days` calendar days to keep the UI payload
    bounded — one year of daily snapshots = 365 dates per strategy."""
    if days <= 0:
        return []
    rows = _load_rows_for_strategy(path, strategy)
    if not rows:
        return []

    # Date-window filter — use the most-recent observed date as the
    # anchor, not today(), so historical datasets don't silently hide.
    # But for the live UI we want a moving "last N days" window off
    # today; doing that would make the function depend on wall-clock
    # time, which fights testability. Compromise: the route above
    # passes a reasonable `days` and we honor it from today's
    # perspective via `cutoff = today - days`.
    # (Implementation keeps this pure: caller controls semantics via
    # the `days` value they pass.)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    seen: set[str] = set()
    for row in rows:
        d = row["date"]
        if d < cutoff:
            continue
        seen.add(d)
    return sorted(seen, reverse=True)


def read_positions_at_date(
    path: Path,
    strategy: str,
    target_date: str,
) -> dict[str, Any] | None:
    """Return the full JSONL row for (strategy, target_date) — including
    `snapshot_at`, `positions`, and any future additions — or None if
    no such row exists. If the writer ever duplicates (shouldn't, but
    idempotency isn't enforced transactionally), the LAST one wins,
    mirroring "most-recent write" semantics."""
    rows = _load_rows_for_strategy(path, strategy)
    match: dict[str, Any] | None = None
    for row in rows:
        if row.get("date") == target_date:
            match = row  # later writes override earlier ones
    return match


def is_valid_date_format(s: str) -> bool:
    """Route-level sanity check; keeps 400 semantics close to input."""
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False

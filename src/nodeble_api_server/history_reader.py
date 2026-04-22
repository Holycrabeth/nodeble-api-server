"""Reader for `~/.nodeble-api/history/daily-pnl.jsonl`.

Paired with `snapshot_writer.py`. Loads the full JSONL in memory,
filters by strategy + date window, sorts ascending by date, and
computes `daily_delta` on the way out — so the frontend chart
receives data in render order (left→right = past→present).

Like `audit_reader`, this module is tolerant: malformed lines and
lines missing key fields are silently skipped. Audit-grade durability
isn't needed here since the underlying state.json stays authoritative
for "what is the strategy holding right now" — this jsonl is
historical telemetry only.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def read_pnl_entries(
    path: Path,
    strategy: str,
    since_date: date | None = None,
) -> list[dict[str, Any]]:
    """Return PnL-history rows for `strategy` on or after `since_date`,
    ascending by date, with a `daily_delta` field appended.

    - Missing file → empty list.
    - Malformed JSON lines / missing `date` / missing `strategy` → skipped.
    - Sort stability doesn't matter (at most one row per date per
      strategy thanks to the writer's idempotency).
    - `daily_delta[i]` = cumulative[i] - cumulative[i-1]. First row's
      delta is None (no prior reference). When either side is null we
      also emit null — honest "no data" beats a misleading 0.
    """
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
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
                d = _parse_date(date_str)
                if d is None:
                    continue
                if since_date is not None and d < since_date:
                    continue
                rows.append(obj)
    except OSError:
        return []

    rows.sort(key=lambda r: r["date"])

    # Compute daily_delta. Scan left-to-right using the previous
    # NON-NULL cumulative value, so a gap day (e.g. weekend when the
    # scheduler didn't fire) uses the last snapshot's cumulative as the
    # baseline rather than introducing a phantom null everywhere.
    prev_cumulative: float | None = None
    for r in rows:
        cum = r.get("realized_pnl_cumulative")
        if cum is None or prev_cumulative is None:
            r["daily_delta"] = None
        else:
            try:
                r["daily_delta"] = round(float(cum) - float(prev_cumulative), 4)
            except (TypeError, ValueError):
                r["daily_delta"] = None
        if isinstance(cum, (int, float)):
            prev_cumulative = float(cum)

    return rows


def compute_since_date(today: date, days: int) -> date:
    """`days=30` today=2026-04-22 → 2026-03-24 (inclusive window start).
    Pure function for the `days` query param → `since_date` mapping. """
    days_int = max(1, int(days))
    return today - timedelta(days=days_int - 1)

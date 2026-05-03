"""ET trading session window calculation for daily-summary endpoint.

Provides `compute_session(now)` returning a SessionInfo dict aligned with
`B1` brainstorm decision: "今日" = 自当日 ET 09:30 开盘起。Pre-market
displays prior session snapshot + next-open countdown.

Contract (TypedDict):
    date_et:     ISO date string for the ET-local date of `now`
    market_open: True iff `now` falls in [09:30, 16:00) ET on a weekday
    next_open:   ISO 8601 UTC of the upcoming market open, or None if
                 market is currently open
    next_close:  ISO 8601 UTC of the upcoming market close (= today's
                 close when market_open=True), or None when market closed

Edge case discipline:
    - 09:30:00 ET exactly → market_open True (inclusive lower bound)
    - 16:00:00 ET exactly → market_open False (exclusive upper bound)
    - Saturday / Sunday → market_open False; next_open = next Monday 09:30 ET
    - Friday after 16:00 → next_open = Monday 09:30 ET (skip weekend)
    - US holidays: NOT handled in v1 MVP (acknowledged backlog item;
      would need pandas-market-calendars or a hardcoded NYSE holiday
      list — defer until real holiday-related bug surfaces).

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §B1
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import TypedDict
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN_ET = time(9, 30)
MARKET_CLOSE_ET = time(16, 0)


class SessionInfo(TypedDict):
    """Shape of compute_session() return value."""

    date_et: str
    market_open: bool
    next_open: str | None
    next_close: str | None


def compute_session(now: datetime) -> SessionInfo:
    """Compute current ET trading session info.

    Args:
        now: Timezone-aware datetime. Caller should pass UTC; we convert
             internally to ET for the comparison. Naive datetime would
             raise on `astimezone()` so input contract is enforced by
             the type system.

    Returns:
        SessionInfo dict with the 4 contract keys.
    """
    now_et = now.astimezone(ET)
    today_et_date = now_et.date()
    open_today = datetime.combine(today_et_date, MARKET_OPEN_ET, tzinfo=ET)
    close_today = datetime.combine(today_et_date, MARKET_CLOSE_ET, tzinfo=ET)

    is_weekday = now_et.weekday() < 5  # Mon=0 ... Fri=4 ; Sat=5, Sun=6
    is_market_open = is_weekday and open_today <= now_et < close_today

    if is_market_open:
        return {
            "date_et": today_et_date.isoformat(),
            "market_open": True,
            "next_open": None,
            "next_close": close_today.astimezone(timezone.utc).isoformat(),
        }

    # Market closed — find next open
    if is_weekday and now_et < open_today:
        # Pre-market on a weekday: next open is later today
        next_open_et = open_today
    else:
        # After-close (any day) OR weekend — next open is the next weekday's 09:30
        candidate = today_et_date + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        next_open_et = datetime.combine(candidate, MARKET_OPEN_ET, tzinfo=ET)

    return {
        "date_et": today_et_date.isoformat(),
        "market_open": False,
        "next_open": next_open_et.astimezone(timezone.utc).isoformat(),
        "next_close": None,
    }

"""Market hours for the Staleness banner. Simple US equity session:
09:30-16:00 ET, Mon-Fri. Holidays are NOT handled in v1 — on a holiday
the banner will show "non-trading hours" which is accurate if technically
noisy. Full NYSE calendar is M5 scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SERVER_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class MarketStatus:
    is_open: bool
    reason: str | None  # "weekend" / "after_hours" / "pre_market" / None
    next_open_iso: str


def _next_open(now: datetime) -> datetime:
    """Return the next trading-session open timestamp (>= now)."""
    # Same day before open
    today_open = now.replace(
        hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0
    )
    if now.weekday() < 5 and now < today_open:
        return today_open

    # Walk forward until next weekday
    day = now.date()
    while True:
        day = day + timedelta(days=1)
        candidate = datetime.combine(day, MARKET_OPEN, tzinfo=SERVER_TZ)
        if candidate.weekday() < 5:
            return candidate


def get_market_status(now: datetime | None = None) -> MarketStatus:
    if now is None:
        now = datetime.now(SERVER_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=SERVER_TZ)
    else:
        now = now.astimezone(SERVER_TZ)

    weekday = now.weekday()
    current_time = now.time()

    if weekday >= 5:
        return MarketStatus(
            is_open=False,
            reason="weekend",
            next_open_iso=_next_open(now).isoformat(),
        )
    if current_time < MARKET_OPEN:
        return MarketStatus(
            is_open=False,
            reason="pre_market",
            next_open_iso=_next_open(now).isoformat(),
        )
    if current_time >= MARKET_CLOSE:
        return MarketStatus(
            is_open=False,
            reason="after_hours",
            next_open_iso=_next_open(now).isoformat(),
        )
    return MarketStatus(is_open=True, reason=None, next_open_iso=now.isoformat())

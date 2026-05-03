"""ET trading session window calculation — tests for Phase 1.1.

Covers:
- Market-open hours (09:30-16:00 ET, weekdays)
- Pre-market (before 09:30 ET on a weekday)
- After-close (after 16:00 ET on a weekday → next weekday open)
- Weekend (Saturday / Sunday → next Monday open)
- Edge: exact open / exact close moments

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §B1
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 1.1
"""
from datetime import datetime, timezone

from nodeble_api_server.aggregators.session import compute_session


def test_during_market_open_returns_market_open_true():
    """09:30-16:00 ET on a weekday → market_open True, next_close populated."""
    # 2026-05-04 14:00 UTC = 10:00 ET Monday (during market hours)
    now = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["date_et"] == "2026-05-04"
    assert s["market_open"] is True
    assert s["next_close"] is not None
    assert s["next_open"] is None


def test_pre_market_returns_market_open_false_with_next_open():
    """Pre-market (before 09:30 ET on a weekday) → market_open False, next_open today."""
    # 2026-05-04 12:00 UTC = 08:00 ET Monday (pre-market)
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is False
    assert s["next_open"] is not None
    assert s["next_close"] is None
    # next_open should be today 13:30 UTC = 09:30 ET
    assert s["next_open"].startswith("2026-05-04T13:30")


def test_after_close_returns_next_open_next_trading_day():
    """After 16:00 ET Friday → next_open is Monday."""
    # 2026-05-01 21:00 UTC Friday = 17:00 ET Friday (after close)
    now = datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is False
    # Next open should be Monday 5/4 (skip weekend)
    assert s["next_open"].startswith("2026-05-04")


def test_weekend_returns_next_monday_open():
    """Saturday / Sunday → next Monday."""
    # 2026-05-02 14:00 UTC Saturday
    now = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is False
    assert s["next_open"].startswith("2026-05-04")


def test_sunday_evening_returns_monday_open():
    """Sunday evening → next Monday open."""
    # 2026-05-03 22:00 UTC Sunday
    now = datetime(2026, 5, 3, 22, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is False
    assert s["next_open"].startswith("2026-05-04")


def test_exact_market_open_moment_is_open():
    """Exactly 09:30 ET → market_open True (inclusive lower bound)."""
    # 2026-05-04 13:30 UTC = 09:30 ET Monday exactly
    now = datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is True


def test_exact_market_close_moment_is_closed():
    """Exactly 16:00 ET → market_open False (exclusive upper bound)."""
    # 2026-05-04 20:00 UTC = 16:00 ET Monday exactly
    now = datetime(2026, 5, 4, 20, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is False


def test_friday_after_close_skips_weekend():
    """Friday 17:00 ET → next_open should be Monday morning, not Saturday."""
    # 2026-05-01 22:00 UTC Friday = 18:00 ET Friday
    now = datetime(2026, 5, 1, 22, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert s["market_open"] is False
    next_open = s["next_open"]
    assert next_open is not None
    # Parse the date portion
    next_open_date = next_open[:10]
    assert next_open_date == "2026-05-04"  # Monday, not Saturday


def test_session_response_shape_has_all_4_keys():
    """Response must always have all 4 keys per the TypedDict contract."""
    now = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    s = compute_session(now)
    assert set(s.keys()) == {"date_et", "market_open", "next_open", "next_close"}

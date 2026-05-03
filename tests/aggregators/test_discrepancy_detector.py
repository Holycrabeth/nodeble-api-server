"""Discrepancy detector tests — Phase 2.1+ (C2 money shot, 4/29-class catch).

Covers `detect_telegram_close_mismatch` (Phase 2.1):
- Positive: 4/29 case (Telegram "Closed 2", ledger 0) → 1 high-severity discrepancy
- Negative: 5/1 case (Telegram "Closed 3", ledger 3) → no discrepancy
- Negative: prior-session Telegram filtered out by session_start cutoff

Covers `detect_stale_state` (Phase 2.2):
- Positive: state.json mtime > 2h during market hours → 1 med-severity discrepancy
- Negative: fresh state mtime → no discrepancy
- Negative: market closed → no flag even if stale

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §C2
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 2.1+2.2
"""
from datetime import datetime, timedelta, timezone

from nodeble_api_server.aggregators.discrepancy_detector import (
    detect_stale_state,
    detect_telegram_close_mismatch,
)


def test_4_29_class_telegram_2_ledger_0_flags_high_severity():
    """The proof case: Telegram says 'Closed 2' but ledger has 0 entries."""
    telegram_messages = [
        {
            "ts": "2026-04-29T19:06:06+00:00",  # 15:06 ET
            "text": "📊 Wheel manage: 10 open, 0 assigned. Closed 2: QQQ:...",
            "bot_id": "wheel",
        },
    ]
    ledger_entries: list[dict] = []  # No close events in ARCH-16 ledger this session
    session_start = "2026-04-29T13:30:00+00:00"  # 09:30 ET

    discrepancies = detect_telegram_close_mismatch(
        bot_id="wheel",
        telegram_messages=telegram_messages,
        ledger_entries=ledger_entries,
        session_start=session_start,
    )
    assert len(discrepancies) == 1
    d = discrepancies[0]
    assert d["type"] == "telegram_close_count_mismatch"
    assert d["bot_id"] == "wheel"
    assert d["severity"] == "high"
    assert "Closed 2" in d["detail"]
    assert "ledger 0" in d["detail"]


def test_matching_telegram_and_ledger_no_discrepancy():
    """5/1 case: Telegram 'Closed 3' matches 3 ledger entries — silent."""
    telegram_messages = [
        {
            "ts": "2026-05-01T17:44:00+00:00",
            "text": "📊 Wheel manage: ... Closed 3: ...",
            "bot_id": "wheel",
        },
    ]
    ledger_entries = [
        {"ts": "2026-05-01T17:44:00+00:00", "event_type": "close", "actor": "wheel"},
        {"ts": "2026-05-01T17:44:01+00:00", "event_type": "close", "actor": "wheel"},
        {"ts": "2026-05-01T17:44:02+00:00", "event_type": "close", "actor": "wheel"},
    ]
    session_start = "2026-05-01T13:30:00+00:00"

    discrepancies = detect_telegram_close_mismatch(
        bot_id="wheel",
        telegram_messages=telegram_messages,
        ledger_entries=ledger_entries,
        session_start=session_start,
    )
    assert discrepancies == []


def test_telegram_outside_session_window_not_counted():
    """Telegram from prior session shouldn't count in today's discrepancy."""
    telegram_messages = [
        {
            "ts": "2026-04-28T17:44:00+00:00",  # prior session
            "text": "Closed 5: ...",
            "bot_id": "wheel",
        },
    ]
    ledger_entries: list[dict] = []
    session_start = "2026-04-29T13:30:00+00:00"

    discrepancies = detect_telegram_close_mismatch(
        bot_id="wheel",
        telegram_messages=telegram_messages,
        ledger_entries=ledger_entries,
        session_start=session_start,
    )
    assert discrepancies == []  # prior-session messages filtered out


# ---------- Phase 2.2: detect_stale_state ----------


def test_stale_state_during_market_hours_flags_med():
    """state.json mtime > 2h during market session → med-severity flag.

    Cron should fire every ~5min during market hours. If the state file
    has been untouched for >2h while the market is open, something is
    silently broken — cron may have died, broker call may be hanging,
    etc. Flag rather than wait for someone to notice manually.
    """
    now = datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc)  # 14:00 ET, mid-session
    stale = (now - timedelta(hours=3)).isoformat()

    discrepancies = detect_stale_state(
        bot_id="wheel", state_mtime=stale, now=now, market_open=True
    )
    assert len(discrepancies) == 1
    d = discrepancies[0]
    assert d["type"] == "stale_state_during_session"
    assert d["bot_id"] == "wheel"
    assert d["severity"] == "med"


def test_fresh_state_during_market_hours_no_discrepancy():
    """Recently updated state (<2h) during market hours → silent."""
    now = datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=30)).isoformat()

    discrepancies = detect_stale_state(
        bot_id="wheel", state_mtime=fresh, now=now, market_open=True
    )
    assert discrepancies == []


def test_stale_state_when_market_closed_no_discrepancy():
    """Market closed → cron isn't expected to run, so stale is fine."""
    now = datetime(2026, 5, 2, 22, 0, tzinfo=timezone.utc)  # 18:00 ET, after close
    stale = (now - timedelta(hours=5)).isoformat()

    discrepancies = detect_stale_state(
        bot_id="wheel", state_mtime=stale, now=now, market_open=False
    )
    assert discrepancies == []

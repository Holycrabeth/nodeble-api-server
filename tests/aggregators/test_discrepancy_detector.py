"""Discrepancy detector tests — Phase 2.1+2.2+2.3 (C2 money shot).

Covers `detect_telegram_close_mismatch` (Phase 2.1):
- Positive: 4/29 case (Telegram "Closed 2", ledger 0) → 1 high-severity discrepancy
- Negative: 5/1 case (Telegram "Closed 3", ledger 3) → no discrepancy
- Negative: prior-session Telegram filtered out by session_start cutoff

Covers `detect_stale_state` (Phase 2.2):
- Positive: state.json mtime > 2h during market hours → 1 med-severity discrepancy
- Negative: fresh state mtime → no discrepancy
- Negative: market closed → no flag even if stale

Covers `detect_missing_cron_run` (Phase 2.3):
- Positive: scheduled fire + 5min grace passed, no log entry → high
- Negative: not yet due (now < expected + grace) → no flag
- Negative: fire happened within grace → no flag
- Negative: weekend → no flag (cron not expected)

Covers `detect_ledger_state_mismatch` (Phase 2.3):
- Positive: state count != ledger count → high
- Negative: counts match → no flag

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §C2
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 2.1+2.2+2.3
"""
from datetime import datetime, time, timedelta, timezone

from nodeble_api_server.aggregators.discrepancy_detector import (
    detect_ledger_state_mismatch,
    detect_missing_cron_run,
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


# ---------- Phase 2.3: detect_missing_cron_run ----------


# Mon 2026-05-04: pick a weekday to anchor the schedule tests.
_SCHEDULE = {
    "signal": time(9, 35),
    "manage": time(9, 43),
    "scan": time(10, 15),
}


def test_missing_cron_signal_after_grace_flags_high():
    """Signal expected at 9:35 ET, now is 9:50 ET, no fire → high-severity flag."""
    # 2026-05-04 13:50 UTC = 09:50 ET Monday (15min after signal expected)
    now = datetime(2026, 5, 4, 13, 50, tzinfo=timezone.utc)
    cron_log_fires: list[str] = []  # nothing fired today

    discrepancies = detect_missing_cron_run(
        bot_id="wheel",
        cron_schedule_et=_SCHEDULE,
        cron_log_fires=cron_log_fires,
        now=now,
    )
    # All 3 (signal, manage — both grace-expired; scan still in future) — only signal+manage
    flagged = [d["type"] for d in discrepancies]
    assert all(t == "missing_cron_run" for t in flagged)
    details = " ".join(d["detail"] for d in discrepancies)
    assert "signal" in details
    assert "manage" in details
    # scan not yet due (10:15 + 5min = 10:20, now is 9:50)
    assert "scan" not in details
    assert all(d["severity"] == "high" for d in discrepancies)
    assert all(d["bot_id"] == "wheel" for d in discrepancies)


def test_missing_cron_not_yet_due_no_flag():
    """Now is 9:30 ET, signal not expected until 9:35 → no flag (not due yet)."""
    # 2026-05-04 13:30 UTC = 09:30 ET Monday
    now = datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc)
    cron_log_fires: list[str] = []

    discrepancies = detect_missing_cron_run(
        bot_id="wheel",
        cron_schedule_et=_SCHEDULE,
        cron_log_fires=cron_log_fires,
        now=now,
    )
    assert discrepancies == []  # everything still future


def test_missing_cron_fire_within_grace_no_flag():
    """Manage scheduled 9:43 ET, fired at 9:46 ET (within 5min grace) → no flag."""
    # 2026-05-04 14:00 UTC = 10:00 ET Monday (well past all schedules)
    now = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    cron_log_fires = [
        "2026-05-04T13:36:00+00:00",  # signal at 09:36 ET (1min late, in grace)
        "2026-05-04T13:46:00+00:00",  # manage at 09:46 ET (3min late, in grace)
        # scan at 10:15 ET still future at 10:00 ET — no flag
    ]
    # only check signal+manage subset (not scan since it's still future)
    schedule = {"signal": time(9, 35), "manage": time(9, 43)}
    discrepancies = detect_missing_cron_run(
        bot_id="wheel",
        cron_schedule_et=schedule,
        cron_log_fires=cron_log_fires,
        now=now,
    )
    assert discrepancies == []


def test_missing_cron_on_weekend_no_flag():
    """Saturday → cron not expected, no flag even with empty fire log."""
    # 2026-05-02 14:00 UTC Saturday
    now = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    discrepancies = detect_missing_cron_run(
        bot_id="wheel",
        cron_schedule_et=_SCHEDULE,
        cron_log_fires=[],
        now=now,
    )
    assert discrepancies == []


# ---------- Phase 2.3: detect_ledger_state_mismatch ----------


def test_ledger_state_mismatch_flags_high():
    """state.json shows 5 closed but ledger has 3 close entries → high-severity."""
    discrepancies = detect_ledger_state_mismatch(
        bot_id="wheel",
        state_close_count=5,
        ledger_close_count=3,
        session_start="2026-05-04T13:30:00+00:00",
    )
    assert len(discrepancies) == 1
    d = discrepancies[0]
    assert d["type"] == "ledger_state_mismatch"
    assert d["severity"] == "high"
    assert d["bot_id"] == "wheel"
    assert "state.json" in d["detail"]
    assert "5" in d["detail"]
    assert "3" in d["detail"]


def test_ledger_state_mismatch_matching_no_flag():
    """state.json matches ledger close count → silent."""
    discrepancies = detect_ledger_state_mismatch(
        bot_id="wheel",
        state_close_count=3,
        ledger_close_count=3,
        session_start="2026-05-04T13:30:00+00:00",
    )
    assert discrepancies == []


def test_ledger_state_mismatch_both_zero_no_flag():
    """Both 0 (no closes today) → silent — common pre-market case."""
    discrepancies = detect_ledger_state_mismatch(
        bot_id="ic",
        state_close_count=0,
        ledger_close_count=0,
        session_start="2026-05-04T13:30:00+00:00",
    )
    assert discrepancies == []

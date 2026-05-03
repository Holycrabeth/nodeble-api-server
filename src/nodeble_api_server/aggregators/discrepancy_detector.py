"""Discrepancy detection logic for daily-summary endpoint (C2 money shot).

Catches 4/29-class bugs: Telegram-reported activity diverges from
ground-truth ARCH-16 ledger. Designed to surface within 60s of fact
(vs ~22h manual catch latency on 4/29).

Each detector is a pure function — caller supplies the parsed sources
(telegram_messages, ledger_entries, state mtimes etc.) and gets back a
list[Discrepancy]. No I/O. Composition lives in `daily_summary.py`.

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §C2
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

# >2h with no state-file write while market is open is the signal that
# cron has silently died (typical fire cadence is every ~5 min). Set
# generously enough that benign skips (e.g. one missed manage cron) don't
# noise the dashboard, but tight enough that a half-day silent failure
# can't go unnoticed.
STALE_STATE_THRESHOLD = timedelta(hours=2)

# Cron grace window: a cron entry scheduled at 09:35 ET is considered
# "fired on time" if any log entry shows up in [09:35, 09:40] ET. 5min
# absorbs typical Tower scheduler jitter + module startup latency without
# masking real failures.
CRON_GRACE_WINDOW = timedelta(minutes=5)

ET = ZoneInfo("America/New_York")


class Discrepancy(TypedDict):
    """One row in `daily-summary.discrepancies[]` per design doc shape."""

    bot_id: str
    type: str
    detail: str
    severity: str  # "high" | "med" | "low"
    detected_at: str


# Matches the canonical Telegram outbox phrasing used by all 4 modules:
#     "Closed N: <symbol>:..."   (manage cron)
#     "Closed 0"                  (rare, edge — still parses)
# Pattern is intentionally tolerant to whitespace; case-insensitive guards
# against future copy edits.
CLOSED_PATTERN = re.compile(r"Closed\s+(\d+)\s*:?", re.IGNORECASE)


def detect_telegram_close_mismatch(
    bot_id: str,
    telegram_messages: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    session_start: str,
) -> list[Discrepancy]:
    """Compare Telegram 'Closed N' claims vs ARCH-16 ledger close entries
    within the current ET trading session.

    The 4/29 case: Wheel manage said "Closed 2" on Telegram but the ARCH-16
    ledger had 0 close events for `actor=wheel` that session — silent
    false-positive that took 22h to catch by hand. With this detector live,
    same divergence surfaces in <60s on the dashboard.

    Args:
        bot_id: One of "ic" / "wheel" / "pmcc" / "directionalspread".
                Used for ledger filter (entry.actor == bot_id) and the
                discrepancy label.
        telegram_messages: List of {ts, text, bot_id} dicts. Caller is
                responsible for sourcing — typically a tail-grep of cron.log
                or a future telegram outbox table. v1 MVP: cron.log tail.
        ledger_entries: List of {ts, event_type, actor, ...} from the
                ARCH-16 ledger (~/.nodeble-pnl/ledger/*.jsonl). Caller
                hands them in pre-loaded.
        session_start: ISO 8601 UTC timestamp of today's 09:30 ET.
                Telegram messages and ledger entries strictly before this
                are filtered out (prior session).

    Returns:
        Empty list if telegram sum == ledger count (healthy).
        Single-element list with `severity=high` discrepancy on mismatch.
        v1 emits at most one per bot per call — we don't multi-flag the
        same divergence with finer-grained sub-reasons; that's Phase 4+
        territory if needed.
    """
    in_session_msgs = [m for m in telegram_messages if m["ts"] >= session_start]
    in_session_ledger = [
        e for e in ledger_entries
        if e["ts"] >= session_start
        and e.get("event_type") == "close"
        and e.get("actor") == bot_id
    ]

    telegram_total = 0
    for msg in in_session_msgs:
        match = CLOSED_PATTERN.search(msg.get("text", ""))
        if match:
            telegram_total += int(match.group(1))

    ledger_total = len(in_session_ledger)

    if telegram_total != ledger_total:
        return [
            {
                "bot_id": bot_id,
                "type": "telegram_close_count_mismatch",
                "detail": (
                    f"{bot_id} Telegram reported Closed {telegram_total} "
                    f"in this session, ledger {ledger_total} close entries — "
                    f"diff {telegram_total - ledger_total}"
                ),
                "severity": "high",
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    return []


def detect_stale_state(
    bot_id: str,
    state_mtime: str,
    now: datetime,
    market_open: bool,
) -> list[Discrepancy]:
    """Flag if state.json hasn't been updated in >2h during a live session.

    During market hours each module's cron (signal / scan / manage) writes
    state every ~5 minutes. If state mtime is > STALE_STATE_THRESHOLD old
    while market_open=True, cron is silently failing — flag med-severity.

    When market_open=False (pre-market, after-close, weekend) cron isn't
    expected to fire, so stale state is benign — skip detection entirely.

    Args:
        bot_id: One of "ic" / "wheel" / "pmcc" / "directionalspread".
        state_mtime: ISO 8601 string of the state.json file mtime
                (caller does the os.stat → datetime conversion).
        now: Current time, timezone-aware UTC.
        market_open: From `compute_session(now).market_open` — caller
                already computed the session window so we don't redo it.

    Returns:
        Empty list if market closed OR state is fresh.
        Single med-severity discrepancy on stale + market open.
    """
    if not market_open:
        return []
    mtime = datetime.fromisoformat(state_mtime)
    if now - mtime > STALE_STATE_THRESHOLD:
        return [
            {
                "bot_id": bot_id,
                "type": "stale_state_during_session",
                "detail": (
                    f"{bot_id} state.json last updated {mtime.isoformat()}, "
                    f">2h during active market session — cron may have failed silently"
                ),
                "severity": "med",
                "detected_at": now.isoformat(),
            }
        ]
    return []


def detect_missing_cron_run(
    bot_id: str,
    cron_schedule_et: dict[str, time],
    cron_log_fires: list[str],
    now: datetime,
) -> list[Discrepancy]:
    """Flag scheduled cron types that should have fired by now but didn't.

    For each (cron_type, expected_time_et) in the schedule:
      - If now is still before expected_time + grace → not yet due, skip
      - Else: scan cron_log_fires for any timestamp in
        [expected_time, expected_time + grace]
      - If no match → high-severity flag

    Weekends short-circuit: on Sat/Sun, cron isn't expected to fire at
    all, so we return an empty list immediately. (Caller could also gate
    by `market_open=False`, but a self-contained weekday check keeps the
    contract simpler.)

    Args:
        bot_id: One of "ic" / "wheel" / "pmcc" / "directionalspread".
        cron_schedule_et: Mapping of cron-type → ET time-of-day, e.g.
                {"signal": time(9, 35), "manage": time(9, 43)}.
                Caller pulls from each module's crontab / config.
        cron_log_fires: ISO 8601 timestamps of all cron fires today
                (any tz; we convert to ET internally). Caller does the
                cron.log tail-grep.
        now: Timezone-aware datetime. Converted to ET for date + grace
                comparisons.

    Returns:
        Empty list if all due crons fired (or it's a weekend).
        One high-severity Discrepancy per missing cron type.
    """
    now_et = now.astimezone(ET)
    today_date = now_et.date()

    if today_date.weekday() >= 5:
        return []  # Sat/Sun — cron not scheduled

    fire_dts_et = [datetime.fromisoformat(t).astimezone(ET) for t in cron_log_fires]

    discrepancies: list[Discrepancy] = []
    detected_at = datetime.now(timezone.utc).isoformat()

    for cron_type, expected_time in cron_schedule_et.items():
        expected_dt = datetime.combine(today_date, expected_time, tzinfo=ET)
        grace_end = expected_dt + CRON_GRACE_WINDOW

        if now_et < grace_end:
            continue  # Not yet due — wait for the next dashboard refresh

        any_fire_in_window = any(
            expected_dt <= fdt <= grace_end for fdt in fire_dts_et
        )
        if not any_fire_in_window:
            discrepancies.append(
                {
                    "bot_id": bot_id,
                    "type": "missing_cron_run",
                    "detail": (
                        f"{bot_id} {cron_type} cron expected at "
                        f"{expected_time.strftime('%H:%M')} ET (+5min grace), "
                        f"no fire logged in window"
                    ),
                    "severity": "high",
                    "detected_at": detected_at,
                }
            )

    return discrepancies


def detect_ledger_state_mismatch(
    bot_id: str,
    state_close_count: int,
    ledger_close_count: int,
    session_start: str,
) -> list[Discrepancy]:
    """Flag if state.json's closed-positions count diverges from ledger.

    Both inputs are pre-filtered by the caller to "this session only"
    (state_close_count = positions closed since session_start; ledger
    likewise filtered to event_type=close + actor=bot_id since
    session_start).

    Different signal class than telegram_close_count_mismatch:
    telegram is "what the bot SAID happened"; this is "what the bot
    THINKS happened internally". State + ledger should converge but
    sometimes don't (state-write race, ledger crash, etc.).

    Args:
        bot_id: Module identifier.
        state_close_count: Closed-position count from state.json this session.
        ledger_close_count: Count of close events in ARCH-16 ledger this session.
        session_start: ISO 8601 UTC for detail message + audit trail.
                Not used for filtering (caller already did it).

    Returns:
        Empty list when counts agree (most cases).
        One high-severity Discrepancy on mismatch.
    """
    if state_close_count == ledger_close_count:
        return []

    return [
        {
            "bot_id": bot_id,
            "type": "ledger_state_mismatch",
            "detail": (
                f"{bot_id} state.json shows {state_close_count} closed positions "
                f"since {session_start}, ledger has {ledger_close_count} close "
                f"entries — diff {state_close_count - ledger_close_count}"
            ),
            "severity": "high",
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }
    ]

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
from datetime import datetime, timezone
from typing import Any, TypedDict


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

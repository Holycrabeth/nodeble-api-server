"""Top-level daily-summary aggregator tests — Phase 3.1.

Wires session helper + 4 detectors + per-bot file I/O into the contract
shape required by the design doc:
    {session, bots[], discrepancies[], sticky[]}

Strategy: use pytest's tmp_path fixture to lay out real fake files
per-bot (cron.log, state.json, optional STOP, shared ledger), pass the
paths into compute_daily_summary, assert on the response. End-to-end
inside one process — no FastAPI client yet (that's Phase 3.2).

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §B+C
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 3.1
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from nodeble_api_server.aggregators.daily_summary import compute_daily_summary


# Mon 2026-05-04 14:00 UTC = 10:00 ET — well into trading session.
NOW = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
SESSION_START_UTC = "2026-05-04T13:30:00+00:00"  # 09:30 ET

SCHEDULE = {
    "signal": time(9, 35),
    "manage": time(9, 43),
    "scan": time(10, 15),
}


def _write_state(state_path: Path, mtime_utc: datetime) -> None:
    """Create a state.json with controlled mtime."""
    state_path.write_text(json.dumps({"positions": [], "today_new_counts": {}}))
    ts = mtime_utc.timestamp()
    os.utime(state_path, (ts, ts))


def _write_cron_log(log_path: Path, lines: list[str]) -> None:
    log_path.write_text("\n".join(lines) + "\n")


def _write_ledger(ledger_path: Path, entries: list[dict]) -> None:
    ledger_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _build_bot_sources(
    tmp_path: Path,
    bot_id: str,
    *,
    cron_log_lines: list[str],
    state_mtime: datetime,
    stop_active: bool = False,
    stop_mtime: datetime | None = None,
) -> dict:
    """Build a bot data source dict with files in tmp_path."""
    bot_dir = tmp_path / bot_id
    bot_dir.mkdir()
    cron_log = bot_dir / "cron.log"
    state_path = bot_dir / "state.json"
    stop_file_path = bot_dir / "STOP"

    _write_cron_log(cron_log, cron_log_lines)
    _write_state(state_path, state_mtime)
    if stop_active:
        stop_file_path.write_text("")
        if stop_mtime is not None:
            ts = stop_mtime.timestamp()
            os.utime(stop_file_path, (ts, ts))

    return {
        "cron_log": str(cron_log),
        "state_path": str(state_path),
        "stop_file_path": str(stop_file_path),
        "cron_schedule_et": SCHEDULE,
        "name": {"ic": "Iron Condor", "wheel": "Wheel", "pmcc": "PMCC", "directionalspread": "Credit Spread"}[bot_id],
        "mode": "live",
    }


def _healthy_cron_log(bot_label: str) -> list[str]:
    """All 3 crons fired on schedule — 09:35/09:43 ET, scan 10:15 not yet due at 10:00."""
    return [
        f"2026-05-04 13:35:02,000 nodeble_{bot_label}.signals.signal_job INFO Signal cron fired",
        f"2026-05-04 13:35:04,290 nodeble_{bot_label}.notify.telegram INFO Telegram message sent: 📊 {bot_label} signal done",
        f"2026-05-04 13:43:02,000 nodeble_{bot_label}.strategy.manager INFO Manage cron fired",
        f"2026-05-04 13:43:17,000 nodeble_{bot_label}.notify.telegram INFO Telegram message sent: 📊 {bot_label} manage: 0 open, 0 assigned. No actions needed",
    ]


def test_happy_path_4_bots_all_healthy(tmp_path):
    """All 4 bots fired cron on time; ledger empty; no STOP; no discrepancies."""
    ledger_path = tmp_path / "ownership_ledger.jsonl"
    _write_ledger(ledger_path, [])  # empty ledger — no opens/closes today

    bot_data_sources = {
        bot_id: {
            **_build_bot_sources(
                tmp_path,
                bot_id,
                cron_log_lines=_healthy_cron_log(bot_id),
                state_mtime=NOW - timedelta(minutes=10),  # fresh state
            ),
            "ledger_path": str(ledger_path),
        }
        for bot_id in ["ic", "wheel", "pmcc", "directionalspread"]
    }

    response = compute_daily_summary(now=NOW, bot_data_sources=bot_data_sources)

    assert response["session"]["market_open"] is True
    assert response["session"]["date_et"] == "2026-05-04"
    assert len(response["bots"]) == 4
    bot_ids = {b["id"] for b in response["bots"]}
    assert bot_ids == {"ic", "wheel", "pmcc", "directionalspread"}

    # No discrepancies on healthy day
    assert response["discrepancies"] == []
    # No sticky entries — all kill switches off
    assert response["sticky"] == []

    # Per-bot sanity: cron_status for signal/manage="ok", scan="pending" (not yet due at 10:00 ET)
    for bot in response["bots"]:
        assert bot["cron_status"]["signal"] == "ok"
        assert bot["cron_status"]["manage"] == "ok"
        assert bot["cron_status"]["scan"] == "pending"
        assert bot["halt"]["active"] is False
        assert bot["today"]["opens"] == 0
        assert bot["today"]["closes"] == 0
        assert bot["today"]["realized_pnl"] == 0.0


def test_4_29_class_telegram_2_ledger_0_surfaces_in_response(tmp_path):
    """End-to-end: a Wheel cron.log with 'Closed 2' but empty ledger → discrepancy."""
    # Ledger has no close events for Wheel today
    ledger_path = tmp_path / "ownership_ledger.jsonl"
    _write_ledger(ledger_path, [])

    # Wheel cron.log has the 4/29 pattern
    wheel_log_lines = _healthy_cron_log("wheel") + [
        "2026-05-04 13:43:18,000 nodeble_wheel.notify.telegram INFO Telegram message sent: 📊 Wheel manage: 8 open, 0 assigned.",
        "Closed 2: SPY:..., QQQ:...",
    ]

    bot_data_sources = {
        "wheel": {
            **_build_bot_sources(
                tmp_path,
                "wheel",
                cron_log_lines=wheel_log_lines,
                state_mtime=NOW - timedelta(minutes=10),
            ),
            "ledger_path": str(ledger_path),
        },
        # Other 3 bots healthy
        **{
            bot_id: {
                **_build_bot_sources(
                    tmp_path,
                    bot_id,
                    cron_log_lines=_healthy_cron_log(bot_id),
                    state_mtime=NOW - timedelta(minutes=10),
                ),
                "ledger_path": str(ledger_path),
            }
            for bot_id in ["ic", "pmcc", "directionalspread"]
        },
    }

    response = compute_daily_summary(now=NOW, bot_data_sources=bot_data_sources)

    # Should surface exactly 1 discrepancy: wheel telegram_close_count_mismatch
    discreps = [d for d in response["discrepancies"] if d["type"] == "telegram_close_count_mismatch"]
    assert len(discreps) == 1
    assert discreps[0]["bot_id"] == "wheel"
    assert discreps[0]["severity"] == "high"


def test_stop_file_active_appears_in_sticky(tmp_path):
    """A bot with STOP file present → sticky 'halt_persisting' entry."""
    ledger_path = tmp_path / "ownership_ledger.jsonl"
    _write_ledger(ledger_path, [])

    stop_set_at = NOW - timedelta(hours=3)  # 3h ago — sticky-worthy
    bot_data_sources = {
        bot_id: {
            **_build_bot_sources(
                tmp_path,
                bot_id,
                cron_log_lines=_healthy_cron_log(bot_id),
                state_mtime=NOW - timedelta(minutes=10),
                stop_active=(bot_id == "ic"),
                stop_mtime=stop_set_at if bot_id == "ic" else None,
            ),
            "ledger_path": str(ledger_path),
        }
        for bot_id in ["ic", "wheel", "pmcc", "directionalspread"]
    }

    response = compute_daily_summary(now=NOW, bot_data_sources=bot_data_sources)

    # ic should show halt.active=True
    ic = next(b for b in response["bots"] if b["id"] == "ic")
    assert ic["halt"]["active"] is True
    assert ic["halt"]["since"] is not None

    # Sticky list should have IC's halt entry
    sticky_ic = [s for s in response["sticky"] if s["bot_id"] == "ic"]
    assert len(sticky_ic) == 1
    assert sticky_ic[0]["type"] == "halt_persisting"


def test_ledger_close_pnl_aggregates_per_bot(tmp_path):
    """Ledger close events for a bot in this session → reflected in today.closes/pnl."""
    ledger_path = tmp_path / "ownership_ledger.jsonl"
    _write_ledger(
        ledger_path,
        [
            # 3 wheel closes today
            {
                "ts": "2026-05-04T13:44:01+00:00",
                "event": "close",
                "strategy": "wheel",
                "realized_pnl": 100.0,
            },
            {
                "ts": "2026-05-04T13:44:02+00:00",
                "event": "close",
                "strategy": "wheel",
                "realized_pnl": 200.0,
            },
            {
                "ts": "2026-05-04T13:44:03+00:00",
                "event": "close",
                "strategy": "wheel",
                "realized_pnl": 50.0,
            },
            # prior session — should NOT count
            {
                "ts": "2026-05-01T18:00:00+00:00",
                "event": "close",
                "strategy": "wheel",
                "realized_pnl": 999.0,
            },
            # different bot — should NOT count for wheel
            {
                "ts": "2026-05-04T13:50:00+00:00",
                "event": "close",
                "strategy": "ic",
                "realized_pnl": 77.0,
            },
        ],
    )

    # Wheel cron.log declares "Closed 3" — matches ledger, no discrepancy
    wheel_log_lines = _healthy_cron_log("wheel") + [
        "2026-05-04 13:44:18,000 nodeble_wheel.notify.telegram INFO Telegram message sent: 📊 Wheel manage: 5 open, 0 assigned. Closed 3: ...",
    ]

    bot_data_sources = {
        "wheel": {
            **_build_bot_sources(
                tmp_path,
                "wheel",
                cron_log_lines=wheel_log_lines,
                state_mtime=NOW - timedelta(minutes=10),
            ),
            "ledger_path": str(ledger_path),
        },
        **{
            bot_id: {
                **_build_bot_sources(
                    tmp_path,
                    bot_id,
                    cron_log_lines=_healthy_cron_log(bot_id),
                    state_mtime=NOW - timedelta(minutes=10),
                ),
                "ledger_path": str(ledger_path),
            }
            for bot_id in ["ic", "pmcc", "directionalspread"]
        },
    }

    response = compute_daily_summary(now=NOW, bot_data_sources=bot_data_sources)

    wheel = next(b for b in response["bots"] if b["id"] == "wheel")
    assert wheel["today"]["closes"] == 3
    assert wheel["today"]["realized_pnl"] == 350.0  # 100 + 200 + 50

    ic = next(b for b in response["bots"] if b["id"] == "ic")
    assert ic["today"]["closes"] == 1
    assert ic["today"]["realized_pnl"] == 77.0

"""Tests for snapshot_writer.take_daily_snapshot + _next_snapshot_time."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nodeble_api_server import snapshot_writer, state_reader

ET = ZoneInfo("America/New_York")


# ── _next_snapshot_time (pure function) ───────────────────────────────────


@pytest.mark.parametrize(
    "now, expected",
    [
        # Before today's 23:59 → returns today's 23:59.
        (datetime(2026, 4, 22, 10, 0, tzinfo=ET), datetime(2026, 4, 22, 23, 59, tzinfo=ET)),
        (datetime(2026, 4, 22, 23, 58, tzinfo=ET), datetime(2026, 4, 22, 23, 59, tzinfo=ET)),
        # Exactly 23:59 → we're "at or past", so next is tomorrow.
        (datetime(2026, 4, 22, 23, 59, tzinfo=ET), datetime(2026, 4, 23, 23, 59, tzinfo=ET)),
        # Just after 23:59 → tomorrow.
        (datetime(2026, 4, 22, 23, 59, 30, tzinfo=ET), datetime(2026, 4, 23, 23, 59, tzinfo=ET)),
        # Post-midnight still returns that day's 23:59.
        (datetime(2026, 4, 23, 0, 5, tzinfo=ET), datetime(2026, 4, 23, 23, 59, tzinfo=ET)),
    ],
)
def test_next_snapshot_time(now, expected):
    assert snapshot_writer._next_snapshot_time(now) == expected


def test_next_snapshot_time_naive_input_treated_as_ET():
    now = datetime(2026, 4, 22, 10, 0)  # no tzinfo
    assert snapshot_writer._next_snapshot_time(now) == datetime(
        2026, 4, 22, 23, 59, tzinfo=ET
    )


# ── take_daily_snapshot ───────────────────────────────────────────────────


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Point strategy state dirs + snapshot file at tmp_path. One fake
    `ic` strategy has a populated state.json; the rest have nothing,
    which is the realistic pre-seed mid-deploy state."""
    home = tmp_path / "home"
    (home / ".nodeble" / "data").mkdir(parents=True)
    (home / ".nodeble" / "data" / "state.json").write_text(
        json.dumps(
            {
                "last_scan_date": "2026-04-22",
                "last_manage_date": "2026-04-22",
                "total_realized_pnl": 161.0,
                "positions": {
                    "SPY_x": {
                        "status": "open",
                        "max_risk": 100,
                        "contracts": 1,
                    }
                },
            }
        )
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    snap_file = tmp_path / "snapshot.jsonl"
    monkeypatch.setattr(snapshot_writer, "_DEFAULT_SNAPSHOT_PATH", snap_file)
    return snap_file


def test_snapshot_writes_one_row_per_strategy(isolated_home: Path):
    now = datetime(2026, 4, 22, 23, 59, tzinfo=ET)
    written = snapshot_writer.take_daily_snapshot(now=now)
    assert len(written) == len(state_reader.STRATEGY_REGISTRY)

    lines = isolated_home.read_text().splitlines()
    assert len(lines) == len(state_reader.STRATEGY_REGISTRY)

    # Every row has the same date + an ISO snapshot_at.
    for line in lines:
        row = json.loads(line)
        assert row["date"] == "2026-04-22"
        assert "snapshot_at" in row and row["snapshot_at"].startswith("2026-04-22")


def test_snapshot_populates_ic_pnl_from_state(isolated_home: Path):
    snapshot_writer.take_daily_snapshot(now=datetime(2026, 4, 22, 23, 59, tzinfo=ET))
    rows = [
        json.loads(ln)
        for ln in isolated_home.read_text().splitlines()
    ]
    ic = next(r for r in rows if r["strategy"] == "ic")
    assert ic["realized_pnl_cumulative"] == 161.0
    assert ic["open_positions_count"] == 1


def test_snapshot_null_pnl_for_strategy_without_state(isolated_home: Path):
    """calendar has no state.json in the fixture — row still written
    but cumulative is null, other fields zero."""
    snapshot_writer.take_daily_snapshot(now=datetime(2026, 4, 22, 23, 59, tzinfo=ET))
    rows = [
        json.loads(ln)
        for ln in isolated_home.read_text().splitlines()
    ]
    cal = next(r for r in rows if r["strategy"] == "calendar")
    assert cal["realized_pnl_cumulative"] is None
    assert cal["open_positions_count"] == 0


def test_snapshot_is_idempotent_same_day(isolated_home: Path):
    now = datetime(2026, 4, 22, 23, 59, tzinfo=ET)
    first = snapshot_writer.take_daily_snapshot(now=now)
    second = snapshot_writer.take_daily_snapshot(now=now)
    # Second call writes nothing — every strategy already has today's row.
    assert len(first) == len(state_reader.STRATEGY_REGISTRY)
    assert second == []
    # File contains exactly len(registry) lines total.
    assert (
        len(isolated_home.read_text().splitlines())
        == len(state_reader.STRATEGY_REGISTRY)
    )


def test_snapshot_writes_new_day_after_date_change(isolated_home: Path):
    snapshot_writer.take_daily_snapshot(now=datetime(2026, 4, 22, 23, 59, tzinfo=ET))
    day1_rows = len(isolated_home.read_text().splitlines())

    # Next day.
    snapshot_writer.take_daily_snapshot(now=datetime(2026, 4, 23, 23, 59, tzinfo=ET))
    day2_rows = len(isolated_home.read_text().splitlines())
    assert day2_rows == 2 * day1_rows


def test_snapshot_partial_new_day_fills_missing_strategies(isolated_home: Path):
    """Simulate a mid-day process crash where only some strategies got
    a row. The next invocation (say on process restart or the ET 23:59
    tick) must backfill the rest for that SAME day."""
    partial = {
        "date": "2026-04-22",
        "snapshot_at": "2026-04-22T12:00:00-04:00",
        "strategy": "ic",
        "realized_pnl_cumulative": 100.0,
        "open_positions_count": 1,
        "budget_used": 0,
        "budget_max": 0,
    }
    isolated_home.parent.mkdir(parents=True, exist_ok=True)
    isolated_home.write_text(json.dumps(partial) + "\n")

    written = snapshot_writer.take_daily_snapshot(
        now=datetime(2026, 4, 22, 23, 59, tzinfo=ET)
    )
    # Wrote every strategy except ic.
    assert len(written) == len(state_reader.STRATEGY_REGISTRY) - 1
    assert all(r["strategy"] != "ic" for r in written)

    lines = isolated_home.read_text().splitlines()
    assert len(lines) == len(state_reader.STRATEGY_REGISTRY)

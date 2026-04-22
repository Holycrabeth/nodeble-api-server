"""Tests for history_reader.read_pnl_entries + /history/pnl route."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, snapshot_writer
from nodeble_api_server.app import app
from nodeble_api_server.history_reader import (
    compute_since_date,
    read_pnl_entries,
)

VALID_TOKEN = "history-test-token"


def _row(date_str: str, cum: float | None, strategy: str = "ic", **over) -> dict:
    return {
        "date": date_str,
        "snapshot_at": f"{date_str}T23:59:00-04:00",
        "strategy": strategy,
        "realized_pnl_cumulative": cum,
        "open_positions_count": 0,
        "budget_used": 0,
        "budget_max": 0,
        **over,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── read_pnl_entries ──────────────────────────────────────────────────────


def test_missing_file_returns_empty(tmp_path: Path):
    assert read_pnl_entries(tmp_path / "none.jsonl", "ic") == []


def test_sorts_ascending_by_date(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(
        path,
        [
            _row("2026-04-22", 100.0),
            _row("2026-04-20", 50.0),
            _row("2026-04-21", 75.0),
        ],
    )
    out = read_pnl_entries(path, "ic")
    assert [r["date"] for r in out] == [
        "2026-04-20",
        "2026-04-21",
        "2026-04-22",
    ]


def test_strategy_filter(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(
        path,
        [
            _row("2026-04-22", 100.0, strategy="ic"),
            _row("2026-04-22", 200.0, strategy="wheel"),
        ],
    )
    out = read_pnl_entries(path, "ic")
    assert len(out) == 1
    assert out[0]["strategy"] == "ic"


def test_since_date_window(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(
        path,
        [
            _row("2026-04-01", 10.0),
            _row("2026-04-15", 50.0),
            _row("2026-04-22", 100.0),
        ],
    )
    out = read_pnl_entries(path, "ic", since_date=date(2026, 4, 10))
    dates = [r["date"] for r in out]
    assert dates == ["2026-04-15", "2026-04-22"]


def test_daily_delta_first_row_is_null(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(path, [_row("2026-04-22", 100.0)])
    out = read_pnl_entries(path, "ic")
    assert out[0]["daily_delta"] is None


def test_daily_delta_subsequent_computed(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(
        path,
        [
            _row("2026-04-20", 50.0),
            _row("2026-04-21", 75.0),
            _row("2026-04-22", 100.0),
        ],
    )
    out = read_pnl_entries(path, "ic")
    deltas = [r["daily_delta"] for r in out]
    assert deltas == [None, 25.0, 25.0]


def test_daily_delta_skips_over_null_cumulative(tmp_path: Path):
    """A null cumulative day (e.g. state.json unreadable that snapshot)
    gets a null delta; the NEXT non-null day computes its delta from
    the LAST seen non-null cumulative."""
    path = tmp_path / "h.jsonl"
    _write(
        path,
        [
            _row("2026-04-20", 50.0),
            _row("2026-04-21", None),
            _row("2026-04-22", 80.0),
        ],
    )
    out = read_pnl_entries(path, "ic")
    deltas = [r["daily_delta"] for r in out]
    # day1 null (first); day2 null (cumulative missing); day3 = 80-50=30.
    assert deltas == [None, None, 30.0]


def test_malformed_line_skipped(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(_row("2026-04-22", 100.0)) + "\n")
        f.write("not valid json\n")
        f.write(json.dumps(_row("2026-04-21", 90.0)) + "\n")
    out = read_pnl_entries(path, "ic")
    assert len(out) == 2


def test_missing_required_fields_skipped(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps({"strategy": "ic"}) + "\n")  # missing date
        f.write(json.dumps({"date": "bad-date", "strategy": "ic"}) + "\n")
        f.write(json.dumps(_row("2026-04-22", 100.0)) + "\n")
    out = read_pnl_entries(path, "ic")
    assert len(out) == 1


# ── compute_since_date ────────────────────────────────────────────────────


def test_compute_since_date_inclusive():
    assert compute_since_date(date(2026, 4, 22), 30) == date(2026, 3, 24)
    assert compute_since_date(date(2026, 4, 22), 1) == date(2026, 4, 22)
    # Clamped to >=1.
    assert compute_since_date(date(2026, 4, 22), 0) == date(2026, 4, 22)


# ── Route ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_snapshot(tmp_path, monkeypatch):
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [{"token": VALID_TOKEN, "label": "t"}],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    snap_file = tmp_path / "snap" / "daily-pnl.jsonl"
    monkeypatch.setattr(snapshot_writer, "_DEFAULT_SNAPSHOT_PATH", snap_file)

    return TestClient(app), snap_file


def test_route_404_unknown_strategy(client_with_snapshot):
    client, _ = client_with_snapshot
    r = client.get(
        "/api/v1/strategies/bogus/history/pnl",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 404


def test_route_requires_auth(client_with_snapshot):
    client, _ = client_with_snapshot
    r = client.get("/api/v1/strategies/ic/history/pnl")
    assert r.status_code == 401


def test_route_empty_when_no_data(client_with_snapshot):
    client, _ = client_with_snapshot
    r = client.get(
        "/api/v1/strategies/ic/history/pnl",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.json() == {"strategy": "ic", "entries": []}


def test_route_returns_entries_with_delta(client_with_snapshot):
    client, snap_file = client_with_snapshot
    _write(
        snap_file,
        [
            _row("2026-04-20", 50.0),
            _row("2026-04-21", 75.0),
            _row("2026-04-22", 100.0),
        ],
    )
    r = client.get(
        "/api/v1/strategies/ic/history/pnl?days=365",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    body = r.json()
    assert body["strategy"] == "ic"
    assert len(body["entries"]) == 3
    assert [e["date"] for e in body["entries"]] == [
        "2026-04-20",
        "2026-04-21",
        "2026-04-22",
    ]
    deltas = [e["daily_delta"] for e in body["entries"]]
    assert deltas == [None, 25.0, 25.0]


def test_route_days_clamped_to_365(client_with_snapshot):
    client, snap_file = client_with_snapshot
    _write(snap_file, [_row("2026-04-22", 100.0)])
    r = client.get(
        "/api/v1/strategies/ic/history/pnl?days=9999",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    # Doesn't 400; clamp happens silently server-side.

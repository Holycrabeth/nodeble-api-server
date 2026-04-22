"""Tests for positions_history_reader + /history/positions route."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, snapshot_writer
from nodeble_api_server.app import app
from nodeble_api_server.positions_history_reader import (
    is_valid_date_format,
    read_available_dates,
    read_positions_at_date,
)

VALID_TOKEN = "pos-history-test-token"


def _row(
    date_str: str,
    *,
    strategy: str = "ic",
    positions: list | None = None,
) -> dict:
    return {
        "date": date_str,
        "snapshot_at": f"{date_str}T23:59:00-04:00",
        "strategy": strategy,
        "positions": positions if positions is not None else [],
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── is_valid_date_format ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "s, expected",
    [
        ("2026-04-22", True),
        ("2026-12-31", True),
        ("2026-13-01", False),  # bad month
        ("2026-04-32", False),  # bad day
        ("2026-4-22", False),   # not zero-padded
        ("26-04-22", False),    # wrong length
        ("", False),
        ("not a date", False),
    ],
)
def test_is_valid_date_format(s, expected):
    assert is_valid_date_format(s) is expected


# ── read_available_dates ──────────────────────────────────────────────────


def test_available_dates_missing_file_returns_empty(tmp_path: Path):
    assert read_available_dates(tmp_path / "none.jsonl", "ic") == []


def test_available_dates_newest_first_deduped(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    today = date.today()
    _write(
        path,
        [
            _row((today - timedelta(days=2)).isoformat()),
            _row((today - timedelta(days=0)).isoformat()),
            _row((today - timedelta(days=0)).isoformat()),  # dup
            _row((today - timedelta(days=1)).isoformat()),
        ],
    )
    out = read_available_dates(path, "ic", days=90)
    assert out == [
        today.isoformat(),
        (today - timedelta(days=1)).isoformat(),
        (today - timedelta(days=2)).isoformat(),
    ]


def test_available_dates_days_window_clamps_old(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    today = date.today()
    _write(
        path,
        [
            _row((today - timedelta(days=200)).isoformat()),
            _row((today - timedelta(days=5)).isoformat()),
            _row(today.isoformat()),
        ],
    )
    out = read_available_dates(path, "ic", days=10)
    # 200 days ago gets cut; 5 days ago + today survive.
    assert len(out) == 2
    assert (today - timedelta(days=200)).isoformat() not in out


def test_available_dates_strategy_filter(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    today = date.today()
    _write(
        path,
        [
            _row(today.isoformat(), strategy="ic"),
            _row(today.isoformat(), strategy="wheel"),
        ],
    )
    assert read_available_dates(path, "ic") == [today.isoformat()]
    assert read_available_dates(path, "wheel") == [today.isoformat()]


def test_available_dates_days_zero_returns_empty(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(path, [_row(date.today().isoformat())])
    assert read_available_dates(path, "ic", days=0) == []


# ── read_positions_at_date ────────────────────────────────────────────────


def test_read_positions_missing_file_returns_none(tmp_path: Path):
    assert read_positions_at_date(tmp_path / "nope.jsonl", "ic", "2026-04-22") is None


def test_read_positions_exact_match(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    positions = [{"id": "pos1", "status": "open"}]
    _write(
        path,
        [
            _row("2026-04-21", positions=[]),
            _row("2026-04-22", positions=positions),
        ],
    )
    row = read_positions_at_date(path, "ic", "2026-04-22")
    assert row is not None
    assert row["positions"] == positions
    assert row["snapshot_at"] == "2026-04-22T23:59:00-04:00"


def test_read_positions_no_match_returns_none(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    _write(path, [_row("2026-04-22")])
    assert read_positions_at_date(path, "ic", "2026-04-20") is None


def test_read_positions_last_write_wins_on_duplicate(tmp_path: Path):
    """Defensive — writer shouldn't produce dups, but if something
    bypasses idempotency we return the last entry instead of silently
    dropping one or crashing."""
    path = tmp_path / "h.jsonl"
    _write(
        path,
        [
            _row("2026-04-22", positions=[{"id": "first"}]),
            _row("2026-04-22", positions=[{"id": "second"}]),
        ],
    )
    row = read_positions_at_date(path, "ic", "2026-04-22")
    assert row["positions"] == [{"id": "second"}]


def test_read_positions_malformed_line_skipped(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("not json at all\n")
        f.write(json.dumps(_row("2026-04-22", positions=[{"x": 1}])) + "\n")
    row = read_positions_at_date(path, "ic", "2026-04-22")
    assert row is not None
    assert row["positions"] == [{"x": 1}]


# ── Route ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_pos(tmp_path, monkeypatch):
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
    pos_file = tmp_path / "pos" / "daily-positions.jsonl"
    monkeypatch.setattr(
        snapshot_writer, "_DEFAULT_POSITIONS_SNAPSHOT_PATH", pos_file
    )
    return TestClient(app), pos_file


def _auth():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def test_route_404_unknown_strategy(client_with_pos):
    client, _ = client_with_pos
    r = client.get("/api/v1/strategies/bogus/history/positions", headers=_auth())
    assert r.status_code == 404


def test_route_requires_auth(client_with_pos):
    client, _ = client_with_pos
    r = client.get("/api/v1/strategies/ic/history/positions")
    assert r.status_code == 401


def test_route_400_on_malformed_date(client_with_pos):
    client, _ = client_with_pos
    r = client.get(
        "/api/v1/strategies/ic/history/positions?date=not-a-date",
        headers=_auth(),
    )
    assert r.status_code == 400


def test_route_empty_when_no_snapshots(client_with_pos):
    client, _ = client_with_pos
    r = client.get(
        "/api/v1/strategies/ic/history/positions", headers=_auth()
    )
    body = r.json()
    assert body["positions"] == []
    assert body["available_dates"] == []
    assert body["snapshot_at"] is None
    assert body["requested_date"] is None


def test_route_default_returns_latest_date(client_with_pos):
    client, pos_file = client_with_pos
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_s = today.isoformat()
    _write(
        pos_file,
        [
            _row(yesterday, positions=[{"id": "old"}]),
            _row(today_s, positions=[{"id": "new"}]),
        ],
    )
    r = client.get(
        "/api/v1/strategies/ic/history/positions", headers=_auth()
    ).json()
    assert r["requested_date"] == today_s
    assert r["positions"] == [{"id": "new"}]
    assert r["available_dates"] == [today_s, yesterday]


def test_route_explicit_date(client_with_pos):
    client, pos_file = client_with_pos
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    _write(
        pos_file,
        [
            _row(yesterday, positions=[{"id": "old"}]),
            _row(today.isoformat(), positions=[{"id": "new"}]),
        ],
    )
    r = client.get(
        f"/api/v1/strategies/ic/history/positions?date={yesterday}",
        headers=_auth(),
    ).json()
    assert r["requested_date"] == yesterday
    assert r["positions"] == [{"id": "old"}]


def test_route_unknown_date_returns_empty_positions(client_with_pos):
    client, pos_file = client_with_pos
    today = date.today()
    _write(pos_file, [_row(today.isoformat(), positions=[{"x": 1}])])
    # Valid date format, but no snapshot exists that day.
    far_past = (today - timedelta(days=30)).isoformat()
    r = client.get(
        f"/api/v1/strategies/ic/history/positions?date={far_past}",
        headers=_auth(),
    ).json()
    assert r["requested_date"] == far_past
    assert r["positions"] == []
    assert r["snapshot_at"] is None
    # available_dates still populated so the UI can offer other options.
    assert r["available_dates"] == [today.isoformat()]

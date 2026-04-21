"""Tests for US equity market-hours logic."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config
from nodeble_api_server.app import app
from nodeble_api_server.market import get_market_status

ET = ZoneInfo("America/New_York")

VALID_TOKEN = "market-test-token"


@pytest.mark.parametrize(
    "dt, expect_open, expect_reason",
    [
        # Monday 10:30 ET — mid-session.
        (datetime(2026, 4, 20, 10, 30, tzinfo=ET), True, None),
        # Monday 09:29 ET — pre-market.
        (datetime(2026, 4, 20, 9, 29, tzinfo=ET), False, "pre_market"),
        # Monday 16:00 ET — closed (edge: close boundary inclusive-off).
        (datetime(2026, 4, 20, 16, 0, tzinfo=ET), False, "after_hours"),
        # Monday 16:01 ET — after hours.
        (datetime(2026, 4, 20, 16, 1, tzinfo=ET), False, "after_hours"),
        # Saturday — weekend.
        (datetime(2026, 4, 25, 11, 0, tzinfo=ET), False, "weekend"),
        # Sunday — weekend.
        (datetime(2026, 4, 26, 11, 0, tzinfo=ET), False, "weekend"),
        # Friday 15:59 ET — still open.
        (datetime(2026, 4, 24, 15, 59, tzinfo=ET), True, None),
    ],
)
def test_market_status_classification(dt: datetime, expect_open: bool, expect_reason):
    s = get_market_status(now=dt)
    assert s.is_open is expect_open
    assert s.reason == expect_reason
    assert s.next_open_iso  # always populated


def test_next_open_skips_weekend():
    # Friday 16:01 → next open should be Monday 09:30
    fri_evening = datetime(2026, 4, 24, 16, 1, tzinfo=ET)
    s = get_market_status(now=fri_evening)
    assert s.is_open is False
    assert "2026-04-27T09:30" in s.next_open_iso  # Monday


def test_next_open_saturday_goes_to_monday():
    sat = datetime(2026, 4, 25, 11, 0, tzinfo=ET)
    s = get_market_status(now=sat)
    assert "2026-04-27T09:30" in s.next_open_iso


def test_next_open_premarket_same_day():
    mon_dawn = datetime(2026, 4, 20, 7, 0, tzinfo=ET)
    s = get_market_status(now=mon_dawn)
    assert "2026-04-20T09:30" in s.next_open_iso


# ── Route ──────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [{"token": VALID_TOKEN, "label": "test"}],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    return TestClient(app)


def test_route_requires_auth(client):
    r = client.get("/api/v1/market/status")
    assert r.status_code == 401


def test_route_returns_shape(client):
    r = client.get(
        "/api/v1/market/status",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "is_open" in body
    assert "reason" in body
    assert "next_open_iso" in body
    assert isinstance(body["is_open"], bool)

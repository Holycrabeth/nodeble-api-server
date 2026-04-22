"""Route tests for /api/v1/strategies/*."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.state_reader import STRATEGY_REGISTRY

VALID_TOKEN = "routes-test-token"


@pytest.fixture(autouse=True)
def _reset_cache():
    state_reader.clear_cache()
    yield
    state_reader.clear_cache()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Layout a fake HOME with IC + Wheel strategies and an orchestrator allocation."""
    # IC: 2 open, 1 closed
    ic_dir = tmp_path / ".nodeble"
    (ic_dir / "data").mkdir(parents=True)
    (ic_dir / "config").mkdir(parents=True)
    (ic_dir / "data" / "state.json").write_text(json.dumps({
        "last_scan_date": "2026-04-19",
        "last_manage_date": "2026-04-19T14:30:00",
        "positions": {
            "SPY_ic_001": {"status": "open", "max_risk": 100, "contracts": 1, "underlying": "SPY"},
            "SPY_ic_002": {"status": "open", "max_risk": 200, "contracts": 1, "underlying": "SPY"},
            "SPY_ic_003": {"status": "close_profit", "max_risk": 150, "contracts": 1},
        },
    }))
    (ic_dir / "config" / "strategy.yaml").write_text(yaml.safe_dump({
        "mode": "live",
        "capital": {"budget": 20000},
    }))
    (ic_dir / "config" / "risk.yaml").write_text(yaml.safe_dump({
        "risk": {"max_concurrent_positions": 8},
    }))
    (ic_dir / "data" / "signal_state.json").write_text(json.dumps({
        "generated_at": "2026-04-19T13:00:00-04:00",
    }))

    # Wheel: positions as dict with mixed statuses
    wheel_dir = tmp_path / ".nodeble-wheel"
    (wheel_dir / "data").mkdir(parents=True)
    (wheel_dir / "config").mkdir(parents=True)
    (wheel_dir / "data" / "state.json").write_text(json.dumps({
        "last_scan_date": "2026-04-19",
        "last_manage_date": "2026-04-19",
        "positions": {
            "SPY_csp_001": {"status": "open", "max_risk": 5000, "contracts": 1},
            "SPY_csp_002": {"status": "assigned", "max_risk": 5000, "contracts": 1},
            "SPY_cc_001": {"status": "closed_profit", "max_risk": 100, "contracts": 1},
        },
    }))
    (wheel_dir / "config" / "strategy.yaml").write_text(yaml.safe_dump({
        "mode": "live",
        "capital": {"budget": 100000},
    }))

    # Orchestrator allocation
    alloc_dir = tmp_path / ".nodeble-orchestrator" / "data"
    alloc_dir.mkdir(parents=True)
    alloc_dir.write_text  # noop, ensure dir created
    (alloc_dir / "allocation.json").write_text(json.dumps({
        "strategies": {
            "ic": {"confidence": 60, "allocation_pct": 0.01, "max_buying_power": 5000},
            "wheel": {"confidence": 80, "allocation_pct": 0.8, "max_buying_power": 340000},
        },
    }))

    # Config with the bearer token
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "test"}]},
    }))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    # Redirect Path.home() for this test
    monkeypatch.setenv("HOME", str(tmp_path))
    # On macOS Path.home() also consults pwd module; be thorough
    import os
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(os.environ["HOME"])))

    yield tmp_path


@pytest.fixture
def client(fake_home):
    return TestClient(app)


def _auth(h: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}", **(h or {})}


# ── Auth ────────────────────────────────────────────────────────────────────

def test_list_strategies_requires_auth(client):
    assert client.get("/api/v1/strategies").status_code == 401


def test_get_strategy_requires_auth(client):
    assert client.get("/api/v1/strategies/ic").status_code == 401


def test_get_positions_requires_auth(client):
    assert client.get("/api/v1/strategies/ic/positions").status_code == 401


# ── /api/v1/strategies ──────────────────────────────────────────────────────

def test_list_strategies_only_installed(client):
    r = client.get("/api/v1/strategies", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    ids = sorted(s["id"] for s in data["strategies"])
    assert ids == ["ic", "wheel"]  # only the two we laid out


def test_list_strategies_card_fields(client):
    r = client.get("/api/v1/strategies", headers=_auth()).json()
    ic = next(s for s in r["strategies"] if s["id"] == "ic")
    assert ic["name"] == "Iron Condor"
    assert ic["enabled"] is True
    assert ic["open_positions"] == 2
    assert ic["budget_used"] == 30000.0  # (100+200) * 1 * 100
    assert ic["budget_max"] == 5000      # from allocation.json
    # Date-only normalizes to end-of-day ET so health stays green for
    # ~24h after a date-only run (M2.a follow-up fix).
    assert ic["last_scan_at"] == "2026-04-19T23:59:59-04:00"
    assert ic["last_signal_at"] == "2026-04-19T13:00:00-04:00"
    assert ic["health"] in ("healthy", "warning")  # depends on 'now' vs fixture date
    assert ic["version"] is None
    assert ic["today_pnl"] is None


# ── /api/v1/strategies/{id} ─────────────────────────────────────────────────

def test_get_strategy_includes_config_and_allocation(client):
    r = client.get("/api/v1/strategies/ic", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "ic"
    assert data["config"]["mode"] == "live"
    assert data["config"]["risk"]["max_concurrent_positions"] == 8
    assert data["allocation"]["max_buying_power"] == 5000


def test_get_strategy_unknown_returns_404(client):
    r = client.get("/api/v1/strategies/bogus", headers=_auth())
    assert r.status_code == 404


def test_get_strategy_installed_in_registry_but_no_state_returns_card(client):
    # pmcc is in registry but no state.json in fake_home
    r = client.get("/api/v1/strategies/pmcc", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "pmcc"
    assert data["config"] is None
    assert data["health"] == "critical"  # no scan/manage timestamps


def test_health_stays_critical_even_if_log_mtime_is_recent(fake_home):
    """Regression: the log-mtime fallback is DISPLAY only; it must NOT flip
    an empty-state strategy from 'critical' to 'healthy'.
    Before the Collar fix, log mtime was fed into compute_health, which
    falsely marked stale strategies as healthy just because cron touched a log.
    """
    # pmcc has no state.json but drop a fresh log file
    pmcc_logs = fake_home / ".nodeble-pmcc" / "logs"
    pmcc_logs.mkdir(parents=True, exist_ok=True)
    (pmcc_logs / "nodeble-pmcc.log").write_text("recent cron activity")

    client = TestClient(app)
    r = client.get("/api/v1/strategies/pmcc", headers=_auth()).json()
    # Display timestamps populated from log mtime (user can still see "last log touch")
    assert r["last_scan_at"] is not None
    # But health must stay critical because state.json is missing
    assert r["health"] == "critical"


# ── /api/v1/strategies/{id}/positions ───────────────────────────────────────

def test_get_positions_returns_array_with_spread_id(client):
    r = client.get("/api/v1/strategies/ic/positions", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    positions = data["positions"]
    assert isinstance(positions, list)
    assert len(positions) == 3
    for p in positions:
        assert "spread_id" in p


def test_get_positions_wheel_dict_converted_to_list(client):
    r = client.get("/api/v1/strategies/wheel/positions", headers=_auth())
    data = r.json()
    assert isinstance(data["positions"], list)
    ids = sorted(p["spread_id"] for p in data["positions"])
    assert ids == ["SPY_cc_001", "SPY_csp_001", "SPY_csp_002"]


def test_get_positions_unknown_returns_404(client):
    assert client.get("/api/v1/strategies/bogus/positions", headers=_auth()).status_code == 404


def test_get_positions_installed_no_state_returns_empty(client):
    r = client.get("/api/v1/strategies/pmcc/positions", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"positions": []}


# ── directionalspread → allocation_key 'cs' ─────────────────────────────────

def test_directionalspread_uses_cs_allocation_key(tmp_path, monkeypatch):
    # Fresh home with just directionalspread + allocation using 'cs' key
    ds_dir = tmp_path / ".nodeble-directionalspread"
    (ds_dir / "data").mkdir(parents=True)
    (ds_dir / "config").mkdir(parents=True)
    (ds_dir / "data" / "state.json").write_text(json.dumps({
        "last_scan_date": "2026-04-19",
        "last_manage_date": "2026-04-19",
        "positions": {},
    }))
    (ds_dir / "config" / "strategy.yaml").write_text(yaml.safe_dump({"mode": "live"}))

    alloc_dir = tmp_path / ".nodeble-orchestrator" / "data"
    alloc_dir.mkdir(parents=True)
    (alloc_dir / "allocation.json").write_text(json.dumps({
        "strategies": {"cs": {"max_buying_power": 7777}},
    }))

    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "test"}]},
    }))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    import os
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(os.environ["HOME"])))

    c = TestClient(app)
    r = c.get("/api/v1/strategies/directionalspread", headers=_auth()).json()
    assert r["allocation"]["max_buying_power"] == 7777
    assert r["budget_max"] == 7777

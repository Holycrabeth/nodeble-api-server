"""Tests for M3.b /close-preview + /close routes.

Mirrors test_actions_route.py shape: stub `run_strategy_close` so we
can assert HTTP layer behavior without spawning subprocesses.

Coverage:
  - GET /close-preview: 404 unknown strategy / position, 410 already
    closed, 200 with halt status injection
  - POST /close: 400 confirm_text wrong, 404 unknown strategy, 409
    halted (from current STOP file at request time), 200 forwarding
    subprocess result
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.actions import CloseResult
from nodeble_api_server.app import app
from nodeble_api_server.routes import strategies as strategies_mod


VALID_TOKEN = "close-route-test-token"


def _hdr():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_cache():
    state_reader.clear_cache()
    yield
    state_reader.clear_cache()


@pytest.fixture
def client_with_close_stub(tmp_path: Path, monkeypatch):
    """TestClient wired up with:
    - valid auth token
    - tmp $HOME with state.json containing 1 open position on Wheel
    - run_strategy_close stubbed to record args + return canned CloseResult
    """
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "server": {"host": "127.0.0.1", "port": 8765},
        "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "t"}]},
    }))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    # Minimal home layout — Wheel with 1 open position, IC empty
    for sid, meta in state_reader.STRATEGY_REGISTRY.items():
        cfg_dir = tmp_path / meta["folder"] / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "strategy.yaml").write_text(yaml.safe_dump({"mode": "live"}))

    # Wheel state with 1 open position
    wheel_state_dir = tmp_path / ".nodeble-wheel" / "data"
    wheel_state_dir.mkdir()
    wheel_state_dir.joinpath("state.json").write_text(json.dumps({
        "last_scan_date": "2026-04-26",
        "last_manage_date": "2026-04-26T14:00:00",
        "positions": {
            "SPY   260430P00640000": {
                "position_id": "SPY   260430P00640000",
                "identifier": "SPY   260430P00640000",
                "status": "open",
                "underlying": "SPY",
                "strike": 640.0,
                "contracts": 1,
                "put_call": "PUT",
                "legs": [],
            },
            "SPY   260430C00710000": {
                "position_id": "SPY   260430C00710000",
                "identifier": "SPY   260430C00710000",
                "status": "closed_profit",
                "underlying": "SPY",
                "contracts": 1,
                "legs": [],
            },
        },
    }))

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # Stub the subprocess layer.
    calls: list[dict] = []

    def stub_close(strategy_id, position_id, *, dry_run=False, **kwargs):
        calls.append({
            "strategy_id": strategy_id,
            "position_id": position_id,
            "dry_run": dry_run,
        })
        return CloseResult(
            task_status="completed",
            exit_code=0,
            duration_ms=2345,
            module_payload={
                "status": "completed",
                "position_id": position_id,
                "closed_at": "2026-04-26T14:30:00-04:00",
                "fill_price": 1.20,
                "realized_pnl": 80.0,
                "per_leg_fills": [],
                "error": None,
            },
            stdout_tail="",
            stderr_tail="",
            started_at="2026-04-26T14:29:55-04:00",
            completed_at="2026-04-26T14:30:00-04:00",
            error=None,
        )

    monkeypatch.setattr(strategies_mod, "run_strategy_close", stub_close)

    return TestClient(app), tmp_path, calls


# ── /close-preview ──────────────────────────────────────────────────────────


def test_close_preview_404_unknown_strategy(client_with_close_stub):
    client, _home, _calls = client_with_close_stub
    r = client.get(
        "/api/v1/strategies/does_not_exist/positions/x/close-preview",
        headers=_hdr(),
    )
    assert r.status_code == 404


def test_close_preview_404_unknown_position(client_with_close_stub):
    client, _home, _calls = client_with_close_stub
    r = client.get(
        "/api/v1/strategies/wheel/positions/NONEXISTENT/close-preview",
        headers=_hdr(),
    )
    assert r.status_code == 404


def test_close_preview_410_when_already_closed(client_with_close_stub):
    """Position with status=closed_profit → 410 Gone."""
    client, _home, _calls = client_with_close_stub
    r = client.get(
        "/api/v1/strategies/wheel/positions/SPY   260430C00710000/close-preview",
        headers=_hdr(),
    )
    assert r.status_code == 410


def test_close_preview_returns_position_with_halt_status(client_with_close_stub):
    """Open position → 200 with position info + halted=False (default)."""
    client, _home, _calls = client_with_close_stub
    r = client.get(
        "/api/v1/strategies/wheel/positions/SPY   260430P00640000/close-preview",
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["position_id"] == "SPY   260430P00640000"
    assert body["strategy_id"] == "wheel"
    assert body["status"] == "open"
    assert body["halted"] is False
    assert body["halted_reason"] is None
    # v1: estimated values null, any_quote_missing true
    assert body["estimated_close_value"] is None
    assert body["any_quote_missing"] is True


def test_close_preview_surfaces_halted_when_stop_present(client_with_close_stub):
    """STOP file appears between list cache and close click → halted=True."""
    client, home, _calls = client_with_close_stub
    (home / ".nodeble-wheel" / "STOP").write_text("Drift detected mid-flow")
    r = client.get(
        "/api/v1/strategies/wheel/positions/SPY   260430P00640000/close-preview",
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["halted"] is True
    assert body["halted_reason"] == "Drift detected mid-flow"


# ── POST /close ─────────────────────────────────────────────────────────────


def test_close_400_when_confirm_text_lowercase(client_with_close_stub):
    """confirm_text must be exactly 'CLOSE' case-sensitive."""
    client, _home, _calls = client_with_close_stub
    r = client.post(
        "/api/v1/strategies/wheel/positions/X/close",
        headers=_hdr(),
        json={"confirm_text": "close"},
    )
    assert r.status_code == 400
    assert "case-sensitive" in r.json()["detail"]


def test_close_400_when_confirm_text_typo(client_with_close_stub):
    client, _home, _calls = client_with_close_stub
    r = client.post(
        "/api/v1/strategies/wheel/positions/X/close",
        headers=_hdr(),
        json={"confirm_text": "CLOSe"},
    )
    assert r.status_code == 400


def test_close_404_unknown_strategy(client_with_close_stub):
    client, _home, _calls = client_with_close_stub
    r = client.post(
        "/api/v1/strategies/nonexistent/positions/X/close",
        headers=_hdr(),
        json={"confirm_text": "CLOSE"},
    )
    assert r.status_code == 404


def test_close_409_when_halted(client_with_close_stub):
    """STOP file present at request time → 409 Conflict (NOT cached)."""
    client, home, _calls = client_with_close_stub
    (home / ".nodeble-wheel" / "STOP").write_text("Manual halt before close click")
    r = client.post(
        "/api/v1/strategies/wheel/positions/X/close",
        headers=_hdr(),
        json={"confirm_text": "CLOSE"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "strategy halted"
    assert detail["halted_reason"] == "Manual halt before close click"


def test_close_invokes_subprocess_with_position_id(client_with_close_stub):
    """Valid request reaches the subprocess layer with correct args."""
    client, _home, calls = client_with_close_stub
    r = client.post(
        "/api/v1/strategies/wheel/positions/SPY%20%20%20260430P00640000/close",
        headers=_hdr(),
        json={"confirm_text": "CLOSE", "dry_run": True},
    )
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["strategy_id"] == "wheel"
    assert calls[0]["position_id"] == "SPY   260430P00640000"
    assert calls[0]["dry_run"] is True


def test_close_returns_task_status_from_subprocess(client_with_close_stub):
    """Response body is asdict(CloseResult) — task_status, exit_code, etc."""
    client, _home, _calls = client_with_close_stub
    r = client.post(
        "/api/v1/strategies/wheel/positions/X/close",
        headers=_hdr(),
        json={"confirm_text": "CLOSE"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_status"] == "completed"
    assert body["exit_code"] == 0
    assert body["module_payload"]["fill_price"] == 1.20
    assert body["module_payload"]["realized_pnl"] == 80.0


def test_close_requires_auth(client_with_close_stub):
    client, _home, _calls = client_with_close_stub
    r = client.post(
        "/api/v1/strategies/wheel/positions/X/close",
        json={"confirm_text": "CLOSE"},
    )
    assert r.status_code == 401


def test_close_preview_requires_auth(client_with_close_stub):
    client, _home, _calls = client_with_close_stub
    r = client.get("/api/v1/strategies/wheel/positions/X/close-preview")
    assert r.status_code == 401

"""Tests for GET /api/v1/orchestrator/allocation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app

VALID_TOKEN = "orch-test-token"


@pytest.fixture
def client_with_fake_home(tmp_path: Path, monkeypatch):
    """TestClient rooted at a fake HOME so allocation.json reads land
    under tmp. Mirrors the pattern used by test_killswitch /
    test_strategies_routes."""
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
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state_reader.clear_cache()
    return TestClient(app), tmp_path


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def test_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/orchestrator/allocation")
    assert r.status_code == 401


def test_missing_file_returns_404(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/orchestrator/allocation", headers=_hdr())
    assert r.status_code == 404
    assert "allocation.json" in r.json()["detail"]


def test_returns_allocation_payload_as_is(client_with_fake_home):
    """Server-writes-raw policy: the orchestrator owns the schema; api-
    server pass-through preserves any fields we don't know about yet
    (e.g. account_profile was added post-M3, didn't need backend update)."""
    client, tmp_path = client_with_fake_home

    # Seed the exact shape real orchestrator writes, including the
    # v1.1 account_profile block and fields api-server doesn't
    # reference directly (warnings, spy_stock_*).
    alloc = {
        "date": "2026-04-23",
        "generated_at": "2026-04-23T09:58:02.688046-04:00",
        "regime": "Neutral",
        "composite_score": 0.0868,
        "portfolio_nlv": 460877.82,
        "num_active_strategies": 9,
        "avg_confidence": 54.6,
        "deployable": 435530,
        "dynamic_cash_floor": 0.055,
        "strategies": {
            "ic": {"confidence": 50, "allocation_pct": 0.01, "max_buying_power": 4496},
            "wheel": {"confidence": 58, "allocation_pct": 0.40, "max_buying_power": 344863},
            "cs": {"confidence": 59, "allocation_pct": 0.012, "max_buying_power": 5306},
        },
        "account_profile": {
            "profile_label": "margin",
            "csp_margin_ratio": 0.2,
        },
        "warnings": [],
    }
    path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alloc))

    r = client.get("/api/v1/orchestrator/allocation", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    # Every top-level field we seeded must round-trip, including the
    # ones api-server doesn't consume.
    assert body["regime"] == "Neutral"
    assert body["composite_score"] == 0.0868
    assert body["strategies"]["wheel"]["max_buying_power"] == 344863
    assert body["account_profile"]["profile_label"] == "margin"
    assert body["warnings"] == []

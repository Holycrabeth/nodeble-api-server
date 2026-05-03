"""HTTP integration tests for /api/v1/server/daily-summary — Phase 3.2.

Wires the route + auth + aggregator end-to-end via FastAPI TestClient.
Test isolation: monkeypatches Path.home() to a tmp_path so we don't
read the real Tower bot directories.

What we lock down:
  - 200 + design doc shape (session/bots[]/discrepancies/sticky)
  - 401 without Bearer token
  - Cache-Control: no-store header
  - 4 bots in response (ic / wheel / pmcc / directionalspread)
  - missing files → graceful empty/zero defaults (no 500)

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md §B
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 3.2
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config
from nodeble_api_server.app import app


VALID_TOKEN = "daily-summary-test-token"


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """TestClient with auth configured + Path.home() pinned to tmp_path."""
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "t"}]},
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return TestClient(app)


def _scaffold_bot_dirs(tmp_path: Path) -> None:
    """Make 4 bot dirs + ledger dir empty so the aggregator hits them
    cleanly without surprise file-not-found behavior surfacing."""
    for sub in (
        ".nodeble",
        ".nodeble-wheel",
        ".nodeble-pmcc",
        ".nodeble-directionalspread",
    ):
        (tmp_path / sub / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / sub / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".nodeble-pnl" / "data").mkdir(parents=True, exist_ok=True)


# ── Auth ────────────────────────────────────────────────────────────────────


def test_daily_summary_requires_auth(client):
    r = client.get("/api/v1/server/daily-summary")
    assert r.status_code == 401


# ── Shape ───────────────────────────────────────────────────────────────────


def test_daily_summary_returns_200_with_design_doc_shape(client, tmp_path):
    _scaffold_bot_dirs(tmp_path)

    r = client.get("/api/v1/server/daily-summary", headers=_hdr())
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level keys
    assert set(body.keys()) >= {"session", "bots", "discrepancies", "sticky"}

    # Session sub-keys
    assert set(body["session"].keys()) == {
        "date_et",
        "market_open",
        "next_open",
        "next_close",
    }

    # 4 bots
    assert isinstance(body["bots"], list)
    assert len(body["bots"]) == 4
    bot_ids = {b["id"] for b in body["bots"]}
    assert bot_ids == {"ic", "wheel", "pmcc", "directionalspread"}

    # Per-bot shape
    for bot in body["bots"]:
        assert {"id", "name", "cron_status", "halt", "today", "mode"} <= set(bot.keys())
        assert {"signal", "manage", "scan"} == set(bot["cron_status"].keys())
        assert {"active", "reason", "since"} == set(bot["halt"].keys())
        assert {"opens", "closes", "realized_pnl"} == set(bot["today"].keys())

    assert isinstance(body["discrepancies"], list)
    assert isinstance(body["sticky"], list)


def test_daily_summary_sets_no_store_cache_header(client, tmp_path):
    _scaffold_bot_dirs(tmp_path)
    r = client.get("/api/v1/server/daily-summary", headers=_hdr())
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_daily_summary_handles_missing_files_gracefully(client, tmp_path):
    """No bot dirs at all → still 200, all bots get zeros."""
    # Don't scaffold — Path.home() is tmp_path but no bot dirs exist
    r = client.get("/api/v1/server/daily-summary", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert len(body["bots"]) == 4
    for bot in body["bots"]:
        assert bot["today"]["opens"] == 0
        assert bot["today"]["closes"] == 0
        assert bot["today"]["realized_pnl"] == 0.0
        assert bot["halt"]["active"] is False

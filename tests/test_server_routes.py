"""Tests for /api/v1/server/* routes — Phase A Week 1 stubs.

Per Phase 4.1 contract freeze
(`~/projects/cto/reviews/2026-04-26-phase-4.1-backend-contract-freeze.md`).

13 endpoints across 5 categories — discovery / lifecycle / Tiger creds /
install observability / server logs. These tests pin SHAPE conformance
(real impl Week 2 swaps stubs but must keep shapes for UI 总监 frontend).

What we lock down:
  - 200/202/404/400 status codes per spec
  - response key presence + types
  - install_id idempotency (POST same id twice returns existing)
  - mode=dry_run server-side enforcement (Gap 2 fix)
  - SSE event format (event: ... \\ndata: {...}\\n\\n)
  - auth required (401 without Bearer)

What we do NOT test (yet — real impl Week 2):
  - actual subprocess invocation
  - persistent state across api-server restart
  - Tiger creds storage to disk
  - real journalctl / log fetch
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.routes import server as server_mod


VALID_TOKEN = "server-routes-test-token"


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_install_state():
    """Clear in-memory install state between tests (stub state isn't persisted)."""
    server_mod._INSTALL_STATE.clear()
    server_mod._TIGER_CREDS_STUB = {"exists": False, "account": None, "stored_at": None}
    state_reader.clear_cache()
    yield
    server_mod._INSTALL_STATE.clear()
    server_mod._TIGER_CREDS_STUB = {"exists": False, "account": None, "stored_at": None}


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "server": {"host": "127.0.0.1", "port": 8765},
        "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "t"}]},
    }))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return TestClient(app)


# ── Auth ────────────────────────────────────────────────────────────────────


def test_installed_strategies_requires_auth(client):
    r = client.get("/api/v1/server/installed-strategies")
    assert r.status_code == 401


def test_install_requires_auth(client):
    r = client.post("/api/v1/server/install/wheel", json={"install_id": "x", "config": {}})
    assert r.status_code == 401


# ── Discovery ───────────────────────────────────────────────────────────────


def test_installed_strategies_returns_9_strategies(client):
    """All 9 from STRATEGY_REGISTRY appear with shape per §1.1."""
    r = client.get("/api/v1/server/installed-strategies", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert "strategies" in body
    assert "fetched_at" in body
    cards = body["strategies"]
    assert len(cards) == 9

    expected_ids = {
        "ic", "wheel", "pmcc", "directionalspread", "calendar",
        "collar", "ironbutterfly", "straddle", "strangle",
    }
    assert {c["id"] for c in cards} == expected_ids

    # Schema check on each card
    for card in cards:
        assert set(card.keys()) >= {
            "id", "name", "installed", "status", "installed_at",
            "version", "latest_version_available",
        }
        assert isinstance(card["installed"], bool)
        assert card["status"] in ("running", "paused", "installing", "failed", "not_installed")


def test_strategy_versions_returns_manifest_skeleton(client):
    """Stub returns 9-strategy manifest with placeholder versions."""
    r = client.get("/api/v1/server/strategy-versions", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["manifest_url"] == "https://nodeble.app/releases.json"
    assert body["manifest_unreachable"] is False
    assert "fetched_at" in body
    assert len(body["strategies"]) == 9

    # Each strategy has latest + released_at + changelog_url
    for sid, info in body["strategies"].items():
        assert "latest" in info
        assert "released_at" in info
        assert "changelog_url" in info


# ── Lifecycle: install ──────────────────────────────────────────────────────


def test_install_returns_202_with_urls(client):
    """POST /install returns 202 + sse_url + status_url + log_url."""
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "test-uuid-1", "config": {"budget": 30000}},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["install_id"] == "test-uuid-1"
    assert body["status"] in ("queued", "starting")
    assert body["sse_url"] == "/api/v1/server/install/test-uuid-1/stream"
    assert body["status_url"] == "/api/v1/server/install/test-uuid-1/status"
    assert body["log_url"] == "/api/v1/server/install/test-uuid-1/log"
    assert "started_at" in body


def test_install_404_unknown_strategy(client):
    r = client.post(
        "/api/v1/server/install/nonexistent",
        headers=_hdr(),
        json={"install_id": "x", "config": {}},
    )
    assert r.status_code == 404


def test_install_idempotent_same_install_id(client):
    """Two POSTs with same install_id return same install (no double-spawn)."""
    body = {"install_id": "test-uuid-2", "config": {}}
    r1 = client.post("/api/v1/server/install/wheel", headers=_hdr(), json=body)
    assert r1.status_code == 202
    r2 = client.post("/api/v1/server/install/wheel", headers=_hdr(), json=body)
    # Same install_id returns 200/202 with same install_id
    assert r2.status_code in (200, 202)
    assert r2.json()["install_id"] == "test-uuid-2"


def test_install_enforces_dry_run_mode_server_side(client):
    """Even if frontend POSTs config={mode: live}, backend must override to dry_run.
    Per Phase 4.1 contract §1.2 + UI 总监 Gap 2 fix.
    """
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={
            "install_id": "test-mode-enforce",
            "config": {"mode": "live", "budget": 30000},  # malicious / mistaken frontend
        },
    )
    assert r.status_code == 202
    state = server_mod._INSTALL_STATE["test-mode-enforce"]
    enforced = state["config_with_mode_dry_run_enforced"]
    assert enforced["mode"] == "dry_run", "backend MUST override mode to dry_run"


# ── Lifecycle: validate ─────────────────────────────────────────────────────


def test_validate_returns_valid_true(client):
    r = client.post(
        "/api/v1/server/install/wheel/validate",
        headers=_hdr(),
        json={"config": {"budget": 30000}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["warnings"] == []


def test_validate_404_unknown_strategy(client):
    r = client.post(
        "/api/v1/server/install/nonexistent/validate",
        headers=_hdr(),
        json={"config": {}},
    )
    assert r.status_code == 404


# ── Lifecycle: uninstall / update / pause / resume ──────────────────────────


def test_uninstall_returns_uninstalled_status(client):
    r = client.post("/api/v1/server/uninstall/wheel", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "uninstalled"
    assert "uninstalled_at" in body
    assert "state_archive_path" in body


def test_update_returns_202_like_install(client):
    r = client.post(
        "/api/v1/server/update/wheel",
        headers=_hdr(),
        json={"install_id": "update-uuid-1"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["install_id"] == "update-uuid-1"
    assert body["status"] == "queued"
    assert "sse_url" in body


def test_pause_returns_paused_with_cron_disabled(client):
    r = client.post("/api/v1/server/pause/wheel", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert "paused_at" in body
    assert "signal" in body["cron_disabled"]
    assert "scan" in body["cron_disabled"]


def test_resume_returns_running(client):
    r = client.post("/api/v1/server/resume/wheel", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert "resumed_at" in body


# ── Tiger creds ─────────────────────────────────────────────────────────────


def test_get_tiger_creds_initially_absent(client):
    r = client.get("/api/v1/server/credentials/tiger", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is False
    assert body["account"] is None
    assert body["stored_at"] is None


def test_put_tiger_creds_then_get_shows_account(client):
    r = client.put(
        "/api/v1/server/credentials/tiger",
        headers=_hdr(),
        json={
            "tiger_id": "50691693",
            "tiger_account": "Yongtao_2K1",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE_KEY\n-----END PRIVATE KEY-----",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "stored"
    assert r.json()["account"] == "Yongtao_2K1"

    # GET should now show exists=true with account
    r2 = client.get("/api/v1/server/credentials/tiger", headers=_hdr())
    body = r2.json()
    assert body["exists"] is True
    assert body["account"] == "Yongtao_2K1"
    # CRITICAL: never returns private_key_pem
    assert "private_key_pem" not in body


def test_get_tiger_creds_never_leaks_private_key(client):
    """Security regression guard — even after PUT, GET response must never
    include the private key."""
    client.put(
        "/api/v1/server/credentials/tiger",
        headers=_hdr(),
        json={
            "tiger_id": "x",
            "tiger_account": "y",
            "private_key_pem": "SECRET_PEM_HERE_DO_NOT_LEAK",
        },
    )
    r = client.get("/api/v1/server/credentials/tiger", headers=_hdr())
    serialized = json.dumps(r.json())
    assert "SECRET_PEM_HERE" not in serialized
    assert "private_key_pem" not in serialized


# ── Install observability ───────────────────────────────────────────────────


def test_install_status_404_unknown(client):
    r = client.get("/api/v1/server/install/unknown-id/status", headers=_hdr())
    assert r.status_code == 404


def test_install_status_returns_state_after_install(client):
    """POST /install then GET /status — should see the state."""
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "status-test-1", "config": {}},
    )
    r = client.get("/api/v1/server/install/status-test-1/status", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["install_id"] == "status-test-1"
    assert body["status"] in ("queued", "running", "success", "failed")
    assert "current_step" in body
    assert isinstance(body["steps_completed"], list)
    assert isinstance(body["log_tail"], list)


def test_install_log_404_unknown(client):
    r = client.get("/api/v1/server/install/unknown-id/log", headers=_hdr())
    assert r.status_code == 404


def test_install_log_returns_text_plain(client):
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "log-test-1", "config": {}},
    )
    r = client.get("/api/v1/server/install/log-test-1/log", headers=_hdr())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # Stub log has at least the queued message
    assert "queued" in r.text.lower() or "stub" in r.text.lower()


def test_install_stream_returns_sse_content_type(client):
    """SSE endpoint sets text/event-stream + emits valid event format."""
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "stream-test-1", "config": {}},
    )
    # TestClient consumes streaming response — we read full body
    with client.stream("GET", "/api/v1/server/install/stream-test-1/stream", headers=_hdr()) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > 200:  # got enough to check format
                break
        text = body.decode("utf-8", errors="replace")
        # Per Phase 4.1 contract §2.1 — events have format `event: X\ndata: {...}\n\n`
        assert "event:" in text
        assert "data:" in text


def test_install_stream_unknown_id_emits_failed_complete(client):
    """SSE for unknown install_id emits a 'complete' event with failed status
    rather than 404 (per spec — late subscribers tolerate)."""
    with client.stream("GET", "/api/v1/server/install/never-existed/stream", headers=_hdr()) as r:
        assert r.status_code == 200
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
        text = body.decode("utf-8", errors="replace")
        assert "complete" in text
        assert "failed" in text or "not found" in text


# ── Server logs ─────────────────────────────────────────────────────────────


def test_server_logs_returns_lines(client):
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert "lines" in body
    assert isinstance(body["lines"], list)
    assert "total_returned" in body
    assert "source" in body


def test_server_logs_400_invalid_lines_count(client):
    r = client.get("/api/v1/server/logs/api-server?lines=999", headers=_hdr())
    assert r.status_code == 400


def test_server_logs_400_invalid_level(client):
    r = client.get("/api/v1/server/logs/api-server?level=panic", headers=_hdr())
    assert r.status_code == 400


def test_server_logs_filter_by_level(client):
    """level=error filters to only error-level lines."""
    r = client.get("/api/v1/server/logs/api-server?level=info", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    for line in body["lines"]:
        assert line["level"] == "info"

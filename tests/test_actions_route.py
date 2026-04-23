"""Tests for POST /api/v1/strategies/{id}/actions/scan — the HTTP layer.

Separated from test_actions.py (which covers the subprocess wrapper)
because this layer has different concerns: auth, 404 for unknown
strategy, 400 for missing live-confirm, audit-log side effect, response
serialization.

We swap `run_strategy_scan` for a deterministic stub so we can test
route behavior without spawning subprocesses.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import audit as audit_mod
from nodeble_api_server import config, state_reader
from nodeble_api_server.actions import ScanResult
from nodeble_api_server.app import app
from nodeble_api_server.routes import strategies as strategies_mod


VALID_TOKEN = "test-token-abc"


def _hdr():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
def client_with_scan_stub(tmp_path: Path, monkeypatch):
    """TestClient wired up with:
    - valid auth token
    - tmp $HOME with minimal strategy.yaml files (so strategy_id lookup works)
    - audit file redirected to tmp
    - run_strategy_scan replaced with a stub that records args and returns
      a canned ScanResult. Test cases read the capture list to assert
      argv shape / request body.
    """
    # Valid-token config
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "server": {"host": "127.0.0.1", "port": 8765},
        "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "t"}]},
    }))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    # Minimal home layout so STRATEGY_REGISTRY lookups succeed; content
    # of strategy.yaml doesn't matter for action tests — we stub the
    # subprocess layer entirely.
    for sid, meta in state_reader.STRATEGY_REGISTRY.items():
        cfg_dir = tmp_path / meta["folder"] / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "strategy.yaml").write_text(yaml.safe_dump({"mode": "live"}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # Audit file redirection — real fsync on tmp is cheap.
    audit_file = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setattr(audit_mod, "_DEFAULT_AUDIT_PATH", audit_file)

    # Stub the subprocess layer. Captures each call so tests can assert
    # what the route forwarded.
    calls: list[dict] = []

    def stub_scan(strategy_id, *, mode="dry_run", force=True, **kwargs):
        calls.append({"strategy_id": strategy_id, "mode": mode, "force": force})
        return ScanResult(
            status="success",
            exit_code=0,
            duration_ms=1234,
            stdout_tail="stub scan ok\nno new positions\n",
            stderr_tail="",
            started_at="2026-04-22T20:00:00-04:00",
            completed_at="2026-04-22T20:00:01-04:00",
            error=None,
        )

    monkeypatch.setattr(strategies_mod, "run_strategy_scan", stub_scan)
    state_reader.clear_cache()

    return TestClient(app), calls, audit_file


# ── Auth ────────────────────────────────────────────────────────────────────


def test_requires_auth(client_with_scan_stub):
    client, *_ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "dry_run"},
    )
    assert r.status_code in (401, 403)


# ── 404 for unknown strategy ────────────────────────────────────────────────


def test_unknown_strategy_returns_404(client_with_scan_stub):
    client, calls, _ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/nosuch/actions/scan",
        json={"mode": "dry_run"},
        headers=_hdr(),
    )
    assert r.status_code == 404
    # Subprocess stub should NOT have been called — route must bail first.
    assert calls == []


# ── Happy path: dry_run ────────────────────────────────────────────────────


def test_dry_run_success_returns_result_and_audits(client_with_scan_stub):
    client, calls, audit_file = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "dry_run", "reason": "smoke test"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["exit_code"] == 0
    assert body["duration_ms"] == 1234
    assert "stub scan ok" in body["stdout_tail"]

    # Subprocess layer got the right strategy + mode
    assert len(calls) == 1
    assert calls[0] == {"strategy_id": "wheel", "mode": "dry_run", "force": True}

    # Audit line written, correct fields
    assert audit_file.exists()
    audit_lines = audit_file.read_text().strip().splitlines()
    assert len(audit_lines) == 1
    entry = json.loads(audit_lines[0])
    assert entry["strategy"] == "wheel"
    assert entry["param_path"] == "actions.scan"
    assert entry["old_value"] is None
    assert entry["new_value"]["request"]["mode"] == "dry_run"
    assert entry["new_value"]["result"]["status"] == "success"
    assert entry["reason"] == "smoke test"
    assert entry["result"] == "success"


def test_missing_reason_defaults_to_manual_scan(client_with_scan_stub):
    """If the user doesn't type a reason, dry_run still goes through —
    reason defaults to 'manual scan' in audit so the trail isn't empty."""
    client, _, audit_file = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "dry_run"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    entry = json.loads(audit_file.read_text().strip().splitlines()[-1])
    assert entry["reason"] == "manual scan"


# ── Live-mode gates ─────────────────────────────────────────────────────────


def test_live_without_confirm_returns_400(client_with_scan_stub):
    client, calls, _ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "live", "reason": "real scan"},
        headers=_hdr(),
    )
    assert r.status_code == 400
    assert "confirm" in r.json()["detail"].lower()
    assert calls == []  # subprocess NOT invoked


def test_live_with_empty_reason_returns_400(client_with_scan_stub):
    client, calls, _ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "live", "confirm": True, "reason": "   "},
        headers=_hdr(),
    )
    assert r.status_code == 400
    assert "reason" in r.json()["detail"].lower()
    assert calls == []


def test_live_with_confirm_and_reason_runs(client_with_scan_stub):
    """Both gates satisfied → route proceeds (subprocess still stubbed)."""
    client, calls, _ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={
            "mode": "live",
            "confirm": True,
            "reason": "post-Fed check after market close",
        },
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["mode"] == "live"


# ── Input validation ────────────────────────────────────────────────────────


def test_invalid_mode_returns_422(client_with_scan_stub):
    """pydantic catches unknown mode before our route logic does."""
    client, *_ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "paper", "reason": "oops"},
        headers=_hdr(),
    )
    assert r.status_code == 422


def test_reason_too_long_returns_422(client_with_scan_stub):
    """pydantic max_length=500 guards against abuse."""
    client, *_ = client_with_scan_stub
    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "dry_run", "reason": "x" * 501},
        headers=_hdr(),
    )
    assert r.status_code == 422


# ── Error surfacing ─────────────────────────────────────────────────────────


def test_subprocess_timeout_returns_200_with_error_payload(
    client_with_scan_stub, monkeypatch
):
    """A stuck scan should still return 200 — the request succeeded even
    though the subprocess hit the timeout. The payload carries the
    diagnosis so the UI can render it."""
    client, _, audit_file = client_with_scan_stub

    def timeout_scan(strategy_id, *, mode, force, **kwargs):
        return ScanResult(
            status="timeout",
            exit_code=None,
            duration_ms=30_000,
            stdout_tail="starting scan\n",
            stderr_tail="",
            started_at="2026-04-22T20:00:00-04:00",
            completed_at="2026-04-22T20:00:30-04:00",
            error="scan timed out after 30s",
        )
    monkeypatch.setattr(strategies_mod, "run_strategy_scan", timeout_scan)

    r = client.post(
        "/api/v1/strategies/wheel/actions/scan",
        json={"mode": "dry_run", "reason": "foo"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "timeout"
    assert "timed out" in body["error"]
    # Audit still written with result="timeout" so the trail shows the
    # dead-end, not silent drop.
    entry = json.loads(audit_file.read_text().strip().splitlines()[-1])
    assert entry["result"] == "timeout"
    assert "timed out" in entry["error"]

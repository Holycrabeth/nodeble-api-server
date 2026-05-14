"""Tests for /api/v1/pnl/* — Phase O.B.1 nodeble-pnl surface."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.routes import pnl as pnl_route

VALID_TOKEN = "pnl-test-token"


@pytest.fixture
def client_with_fake_home(tmp_path: Path, monkeypatch):
    """Pattern matches test_orchestrator_route fixture for parity."""
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "server": {"host": "127.0.0.1", "port": 8765},
            "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "t"}]},
        })
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state_reader.clear_cache()
    return TestClient(app), tmp_path


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ── Happy path ──────────────────────────────────────────────────────────────


def test_current_usage_get_passthrough(client_with_fake_home, monkeypatch):
    """Subprocess returns valid JSON → route returns it as-is."""
    client, _ = client_with_fake_home
    fake_payload = {
        "current_usage": {
            "ic": 767.0, "wheel": 67160.0, "pmcc": 8871.0,
            "cs": 0.0, "ironbutterfly": 0.0, "calendar": 0.0,
            "straddle": 0.0, "strangle": 0.0, "collar": 0.0,
        },
        "as_of": "2026-05-05T04:24:05Z",
    }

    def fake_run(args, **kw):
        assert "nodeble_pnl" in args
        assert "--current-usage" in args
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(fake_payload), stderr="")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == fake_payload


def test_current_usage_invokes_correct_python(client_with_fake_home, monkeypatch):
    """Subprocess invocation uses ~/projects/nodeble-pnl/.venv/bin/python."""
    client, tmp_path = client_with_fake_home
    captured = []

    def fake_run(args, **kw):
        captured.extend(args)
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)
    client.get("/api/v1/pnl/current_usage", headers=_hdr())

    expected_python = str(tmp_path / "projects" / "nodeble-pnl" / ".venv" / "bin" / "python")
    assert captured[0] == expected_python


# ── 5xx degradation ─────────────────────────────────────────────────────────


def test_current_usage_subprocess_nonzero_exit_500(client_with_fake_home, monkeypatch):
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="ImportError: missing dep")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 500
    assert "exited 1" in r.json()["detail"]
    assert "ImportError" in r.json()["detail"]


def test_current_usage_subprocess_non_json_500(client_with_fake_home, monkeypatch):
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="not valid json {{", stderr="")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 500
    assert "non-JSON" in r.json()["detail"]


def test_current_usage_subprocess_timeout_504(client_with_fake_home, monkeypatch):
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 504
    assert "timed out" in r.json()["detail"]


def test_current_usage_subprocess_oserror_500(client_with_fake_home, monkeypatch):
    """e.g. PnL python missing on host — fail-loud 500."""
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        raise OSError("[Errno 2] No such file or directory")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 500
    assert "failed to start" in r.json()["detail"]


# ── REAL #17 gate (exit 2 → 503) ────────────────────────────────────────────
# Per PnL Dev PR #2 `431d9b8` + 协作总监 T-20260514-142516: CLI exits 2 when
# any strategy has LIVE positions but no `capital_used_<strategy>` formula
# implemented yet. api-server maps the gate to 503 with strategies_affected
# so downstream can branch on the specific class of service-unavailable.


def test_current_usage_exit_2_returns_503_with_strategies_affected(
    client_with_fake_home, monkeypatch,
):
    client, _ = client_with_fake_home
    # PnL CLI emits diagnostic JSON even on the gated exit-2 path so
    # api-server can surface strategies_affected without re-running.
    gated_stdout = json.dumps({
        "current_usage": {
            "ic": 767.0, "wheel": 0.0, "pmcc": 0.0,
            "cs": 0.0, "ironbutterfly": 0.0, "calendar": 0.0,
            "straddle": 0.0, "strangle": 0.0, "collar": 0.0,
        },
        "as_of": "2026-05-14T14:25:16Z",
        "formula_errors": [
            {"strategy": "wheel", "message": "capital_used_wheel not implemented"},
            {"strategy": "pmcc", "message": "capital_used_pmcc not implemented"},
        ],
    })

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 2, stdout=gated_stdout, stderr="")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 503
    body = r.json()
    # Dispatch contract: body has `error` + `strategies_affected` keys,
    # NOT wrapped in FastAPI's default `{"detail": ...}` shape.
    assert body == {
        "error": "formula_not_implemented",
        "strategies_affected": ["wheel", "pmcc"],
    }


def test_current_usage_exit_2_malformed_stdout_503_empty_list(
    client_with_fake_home, monkeypatch,
):
    """Defensive: gate fired but stdout JSON unparseable. Caller still
    needs to know it's a formula-impl gap (not a generic crash) — return
    503 with empty strategies_affected so caller branches on category +
    server log carries the raw stderr."""
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 2, stdout="not valid json {{", stderr="boom")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 503
    assert r.json() == {
        "error": "formula_not_implemented",
        "strategies_affected": [],
    }


def test_current_usage_exit_2_no_formula_errors_key_503_empty_list(
    client_with_fake_home, monkeypatch,
):
    """Defensive: gate fired (exit 2) but JSON lacks formula_errors key
    (PnL CLI contract drift in a future version, or test fixture gap).
    Still 503 + empty strategies_affected. Caller still gets the right
    error class to branch on."""
    client, _ = client_with_fake_home
    minimal_stdout = json.dumps({"current_usage": {}, "as_of": "x"})

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 2, stdout=minimal_stdout, stderr="")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 503
    assert r.json() == {
        "error": "formula_not_implemented",
        "strategies_affected": [],
    }


def test_current_usage_exit_2_skips_missing_strategy_field(
    client_with_fake_home, monkeypatch,
):
    """Defensive: formula_errors entry lacks `strategy` key (malformed CLI
    output). Skip that entry in strategies_affected — preserve the valid
    ones rather than blowing up the whole response. Mirror the spirit of
    the malformed-JSON path: surface what we can, log the rest."""
    client, _ = client_with_fake_home
    stdout = json.dumps({
        "current_usage": {},
        "as_of": "x",
        "formula_errors": [
            {"strategy": "wheel", "message": "ok"},
            {"message": "no strategy key"},           # silent skip
            {"strategy": "pmcc", "message": "ok"},
        ],
    })

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 2, stdout=stdout, stderr="")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 503
    assert r.json() == {
        "error": "formula_not_implemented",
        "strategies_affected": ["wheel", "pmcc"],
    }


def test_current_usage_exit_3_still_routes_to_500_generic(
    client_with_fake_home, monkeypatch,
):
    """Verify only exit 2 is the special-cased gate. Other non-zero
    exits still hit the generic 500 path (regression guard so a future
    PnL Dev adding exit 3 = something-else doesn't silently get
    misclassified as a formula gate)."""
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 3, stdout="", stderr="some other crash")

    monkeypatch.setattr(pnl_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/pnl/current_usage", headers=_hdr())
    assert r.status_code == 500
    assert "exited 3" in r.json()["detail"]


# ── Auth ────────────────────────────────────────────────────────────────────


def test_current_usage_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/pnl/current_usage")
    assert r.status_code == 401

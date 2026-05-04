"""Tests for /api/v1/orchestrator/* — Phase O.A 5 endpoints."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.routes import orchestrator as orch_route

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


# ── GET /overrides ──────────────────────────────────────────────────────────


def test_overrides_get_missing_file_returns_empty(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/orchestrator/overrides", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"overrides": {}}


def test_overrides_get_returns_file_content(client_with_fake_home):
    client, tmp_path = client_with_fake_home
    p = tmp_path / ".nodeble-orchestrator" / "config" / "overrides.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({
        "overrides": {
            "ic": {"fixed_cap_usd": 50000, "locked": True},
            "wheel": {"fixed_cap_usd": 30000, "locked": False},
        },
        "generated_at": "2026-05-04T00:00:00Z",
    }))
    r = client.get("/api/v1/orchestrator/overrides", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"overrides": {
        "ic": {"fixed_cap_usd": 50000, "locked": True},
        "wheel": {"fixed_cap_usd": 30000, "locked": False},
    }}


def test_overrides_get_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/orchestrator/overrides")
    assert r.status_code == 401


def test_overrides_get_malformed_file_returns_empty(client_with_fake_home):
    """Defensive: garbage yaml → return empty rather than 500."""
    client, tmp_path = client_with_fake_home
    p = tmp_path / ".nodeble-orchestrator" / "config" / "overrides.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[: not yaml")
    r = client.get("/api/v1/orchestrator/overrides", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"overrides": {}}


# ── PUT /overrides ──────────────────────────────────────────────────────────


def test_overrides_put_valid_writes_file(client_with_fake_home):
    client, tmp_path = client_with_fake_home
    payload = {"overrides": {"ic": {"fixed_cap_usd": 50000, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is True
    assert "sum_check_result" in body

    # File written + readable round-trip
    p = tmp_path / ".nodeble-orchestrator" / "config" / "overrides.yaml"
    assert p.exists()
    raw = yaml.safe_load(p.read_text())
    assert raw["overrides"] == {"ic": {"fixed_cap_usd": 50000, "locked": True}}
    assert raw["generated_by"] == "api_server_put"


def test_overrides_put_step_violation_422(client_with_fake_home):
    """协作总监 5/4 PUT contract: 422 + 'cap_step_violation' in detail."""
    client, _ = client_with_fake_home
    payload = {"overrides": {"ic": {"fixed_cap_usd": 50005, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    assert r.status_code == 422
    detail_str = json.dumps(r.json())
    assert "cap_step_violation" in detail_str


def test_overrides_put_negative_422(client_with_fake_home):
    client, _ = client_with_fake_home
    payload = {"overrides": {"ic": {"fixed_cap_usd": -100, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    assert r.status_code == 422


def test_overrides_put_zero_cap_valid(client_with_fake_home):
    """$0 = disable strategy per UX §3.3 — must accept."""
    client, _ = client_with_fake_home
    payload = {"overrides": {"wheel": {"fixed_cap_usd": 0, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    assert r.status_code == 200


def test_overrides_put_replace_semantics(client_with_fake_home):
    """Re-PUT clears strategies not in new payload (per spec §7.2)."""
    client, tmp_path = client_with_fake_home
    client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json={
        "overrides": {"ic": {"fixed_cap_usd": 50000, "locked": True}}
    })
    client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json={
        "overrides": {"wheel": {"fixed_cap_usd": 30000, "locked": False}}
    })
    p = tmp_path / ".nodeble-orchestrator" / "config" / "overrides.yaml"
    raw = yaml.safe_load(p.read_text())
    assert raw["overrides"] == {"wheel": {"fixed_cap_usd": 30000, "locked": False}}


def test_overrides_put_sum_check_no_baseline(client_with_fake_home):
    """No allocation.json yet → sum_check returns ok=null with reason."""
    client, _ = client_with_fake_home
    payload = {"overrides": {"ic": {"fixed_cap_usd": 50000, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    body = r.json()
    assert body["sum_check_result"]["ok"] is None
    assert body["sum_check_result"]["reason"] == "no_baseline_allocation"


def test_overrides_put_sum_check_within_budget(client_with_fake_home):
    """With allocation.json present + caps within budget → ok=True + headroom."""
    client, tmp_path = client_with_fake_home
    alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
    alloc_path.parent.mkdir(parents=True, exist_ok=True)
    alloc_path.write_text(json.dumps({
        "portfolio_nlv": 100000,
        "cash_reserved": 5000,
        "strategies": {
            "ic": {"max_buying_power": 5000},
            "wheel": {"max_buying_power": 80000},
        },
    }))
    state_reader.clear_cache()
    payload = {"overrides": {"ic": {"fixed_cap_usd": 4000, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    body = r.json()
    assert body["sum_check_result"]["ok"] is True
    # caps = 4000 (override) + 80000 (wheel computed) = 84000; cash 5000 → headroom 11000
    assert body["sum_check_result"]["headroom_usd"] == 11000


def test_overrides_put_sum_check_overflow(client_with_fake_home):
    """Caps + cash > NLV → ok=False, headroom negative; PUT still 200 (apply +
    let frontend warn). 协作总监 5/4 example."""
    client, tmp_path = client_with_fake_home
    alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
    alloc_path.parent.mkdir(parents=True, exist_ok=True)
    alloc_path.write_text(json.dumps({
        "portfolio_nlv": 100000,
        "cash_reserved": 5000,
        "strategies": {
            "ic": {"max_buying_power": 5000},
            "wheel": {"max_buying_power": 80000},
        },
    }))
    state_reader.clear_cache()
    payload = {"overrides": {"wheel": {"fixed_cap_usd": 200000, "locked": True}}}
    r = client.put("/api/v1/orchestrator/overrides", headers=_hdr(), json=payload)
    assert r.status_code == 200  # still apply
    body = r.json()
    assert body["sum_check_result"]["ok"] is False
    assert body["sum_check_result"]["headroom_usd"] < 0


def test_overrides_put_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.put(
        "/api/v1/orchestrator/overrides",
        json={"overrides": {"ic": {"fixed_cap_usd": 100, "locked": True}}},
    )
    assert r.status_code == 401


# ── POST /allocate (subprocess-mocked) ──────────────────────────────────────


def test_allocate_post_subprocess_success(client_with_fake_home, monkeypatch):
    """Happy path: subprocess succeeds, fresh allocation.json returned."""
    client, tmp_path = client_with_fake_home

    def fake_run(args, **kw):
        # Simulate orchestrator writing allocation.json + lock file
        alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
        alloc_path.parent.mkdir(parents=True, exist_ok=True)
        alloc_path.write_text(json.dumps({
            "regime": "Neutral",
            "composite_score": 0.05,
            "portfolio_nlv": 100000,
            "strategies": {"ic": {"max_buying_power": 5000}},
        }))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)
    state_reader.clear_cache()

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": True, "force_nlv_refresh": False, "force": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["regime"] == "Neutral"


def test_allocate_post_recently_run_returns_409(client_with_fake_home):
    """Lock file < 60s old + force=False → 409 with lock_ts hint."""
    client, tmp_path = client_with_fake_home
    lock_path = tmp_path / ".nodeble-orchestrator" / "data" / ".allocate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(datetime.now(timezone.utc).isoformat())

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": False},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "allocate_recently_run"
    assert "lock_ts" in detail
    assert "force=true" in detail["hint"]


def test_allocate_post_force_bypasses_lock(client_with_fake_home, monkeypatch):
    """force=True bypasses the lock check."""
    client, tmp_path = client_with_fake_home
    lock_path = tmp_path / ".nodeble-orchestrator" / "data" / ".allocate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(datetime.now(timezone.utc).isoformat())

    def fake_run(args, **kw):
        alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
        alloc_path.parent.mkdir(parents=True, exist_ok=True)
        alloc_path.write_text(json.dumps({"regime": "Neutral", "composite_score": 0.0}))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)
    state_reader.clear_cache()

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": True},
    )
    assert r.status_code == 200


def test_allocate_post_old_lock_no_409(client_with_fake_home, monkeypatch):
    """Lock from 120s ago + force=False → no 409 (outside window)."""
    client, tmp_path = client_with_fake_home
    lock_path = tmp_path / ".nodeble-orchestrator" / "data" / ".allocate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text((datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat())

    def fake_run(args, **kw):
        alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
        alloc_path.parent.mkdir(parents=True, exist_ok=True)
        alloc_path.write_text(json.dumps({"regime": "Neutral"}))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)
    state_reader.clear_cache()

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": False},
    )
    assert r.status_code == 200


def test_allocate_post_subprocess_nonzero_exit_500(client_with_fake_home, monkeypatch):
    """Subprocess exit != 0 → 500 with stderr tail."""
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Tiger API down")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": False},
    )
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["error"] == "allocate_subprocess_nonzero"
    assert "Tiger API down" in detail["stderr_tail"]


def test_allocate_post_subprocess_timeout_504(client_with_fake_home, monkeypatch):
    """Subprocess timeout → 504."""
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, 120)

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": False},
    )
    assert r.status_code == 504


def test_allocate_post_passes_flags_to_subprocess(client_with_fake_home, monkeypatch):
    """Verify the subprocess args include the requested flags."""
    client, tmp_path = client_with_fake_home
    captured_args = []

    def fake_run(args, **kw):
        captured_args.extend(args)
        alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
        alloc_path.parent.mkdir(parents=True, exist_ok=True)
        alloc_path.write_text(json.dumps({"regime": "Neutral"}))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)
    state_reader.clear_cache()

    client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": True, "force_nlv_refresh": True, "force": True},
    )
    assert "--respect-overrides" in captured_args
    assert "--force-nlv-refresh" in captured_args
    # idempotency-window default
    assert any("--idempotency-window=60" in a for a in captured_args)


def test_allocate_post_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.post(
        "/api/v1/orchestrator/allocate",
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": False},
    )
    assert r.status_code == 401


# ── GET /installed-strategies (subprocess-mocked) ───────────────────────────


def test_installed_strategies_get_success(client_with_fake_home, monkeypatch):
    client, _ = client_with_fake_home
    fake_output = {
        "ic": {"installed": True, "has_venv": True, "service_active": True},
        "wheel": {"installed": False, "has_venv": False, "service_active": False},
    }

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(fake_output), stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/orchestrator/installed-strategies", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == fake_output


def test_installed_strategies_get_subprocess_failure_500(client_with_fake_home, monkeypatch):
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 2, stdout="", stderr="ImportError")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/orchestrator/installed-strategies", headers=_hdr())
    assert r.status_code == 500
    assert "exited 2" in r.json()["detail"]


def test_installed_strategies_get_non_json_500(client_with_fake_home, monkeypatch):
    client, _ = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="not json", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.get("/api/v1/orchestrator/installed-strategies", headers=_hdr())
    assert r.status_code == 500
    assert "non-JSON" in r.json()["detail"]


def test_installed_strategies_get_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/orchestrator/installed-strategies")
    assert r.status_code == 401

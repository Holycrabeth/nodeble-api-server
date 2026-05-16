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


# ── Polish round (CTO 5/4 verdict + 协作总监 5/4 dispatch) ──────────────────


def test_allocate_post_clears_cache_returns_fresh_not_stale(
    client_with_fake_home, monkeypatch,
):
    """P2 cache-staleness fix (CTO 5/4 verdict file
    cto/reviews/2026-05-04-orch-phase-oa-post-verify.md).

    Without state_reader.clear_cache() between subprocess success and
    read_allocation(), the response would return the pre-write cached
    version (5s TTL) — frontend Step 4 audit reproduced the stale
    generated_at bug.

    This test: seed allocation.json with old generated_at + populate
    cache, then have subprocess "update" the file with new
    generated_at, then verify POST response carries the NEW
    generated_at (not the cached old one).
    """
    client, tmp_path = client_with_fake_home
    alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
    alloc_path.parent.mkdir(parents=True, exist_ok=True)

    old = {"regime": "Neutral", "generated_at": "2026-05-04T09:58:02-04:00", "stale_marker": True}
    alloc_path.write_text(json.dumps(old))

    # Prime the cache with the OLD value.
    state_reader.clear_cache()
    cached = state_reader.read_allocation()
    assert cached["generated_at"] == "2026-05-04T09:58:02-04:00"

    # Now subprocess "writes" a NEW allocation.json (later timestamp).
    new = {"regime": "Bullish", "generated_at": "2026-05-04T10:15:00-04:00", "stale_marker": False}

    def fake_run(args, **kw):
        alloc_path.write_text(json.dumps(new))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": False, "force_nlv_refresh": False, "force": True},
    )
    assert r.status_code == 200
    body = r.json()
    # The response must reflect the FRESH file content (cache cleared),
    # not the cached OLD content.
    assert body["generated_at"] == "2026-05-04T10:15:00-04:00", (
        f"stale cache leaked into response: got {body['generated_at']}"
    )
    assert body["regime"] == "Bullish"
    assert body["stale_marker"] is False

    # Subsequent GET /allocation also sees fresh (cache stays cleared).
    r2 = client.get("/api/v1/orchestrator/allocation", headers=_hdr())
    assert r2.json()["generated_at"] == "2026-05-04T10:15:00-04:00"


def test_overrides_put_no_envelope_rejected_422(client_with_fake_home):
    """extra='forbid' lesson #52: missing top-level `overrides:` envelope
    must 422, not silently no-op. Frontend audit 5/4 lost time chasing
    a phantom backend bug because PUT silently accepted curl shape
    `{ic: {...}}` with extra='ignore' default."""
    client, _ = client_with_fake_home
    r = client.put(
        "/api/v1/orchestrator/overrides", headers=_hdr(),
        json={"ic": {"fixed_cap_usd": 100, "locked": False}},  # no "overrides:" envelope
    )
    assert r.status_code == 422


def test_overrides_put_extra_field_rejected_422(client_with_fake_home):
    """extra='forbid' on OverridesIn — typo'd top-level fields surface as 422."""
    client, _ = client_with_fake_home
    r = client.put(
        "/api/v1/orchestrator/overrides", headers=_hdr(),
        json={
            "overrides": {"ic": {"fixed_cap_usd": 100, "locked": True}},
            "typo_field": "value",
        },
    )
    assert r.status_code == 422


def test_overrides_put_extra_per_strategy_field_rejected_422(client_with_fake_home):
    """extra='forbid' on OverrideCap — typo inside a strategy entry surfaces as 422."""
    client, _ = client_with_fake_home
    r = client.put(
        "/api/v1/orchestrator/overrides", headers=_hdr(),
        json={"overrides": {"ic": {
            "fixed_cap_usd": 100,
            "locked": True,
            "lockd": True,  # typo of `locked`
        }}},
    )
    assert r.status_code == 422


def test_allocate_post_extra_field_rejected_422(client_with_fake_home):
    """extra='forbid' on AllocateIn — typo'd flag (e.g. force_relfresh) must 422."""
    client, _ = client_with_fake_home
    r = client.post(
        "/api/v1/orchestrator/allocate", headers=_hdr(),
        json={"respect_overrides": True, "junk": 1},
    )
    assert r.status_code == 422


def test_overrides_get_still_200_regression(client_with_fake_home):
    """Regression check: extra='forbid' is on input models only; GET /overrides
    response shape unchanged."""
    client, tmp_path = client_with_fake_home
    p = tmp_path / ".nodeble-orchestrator" / "config" / "overrides.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({
        "overrides": {"ic": {"fixed_cap_usd": 50000, "locked": True}},
    }))
    r = client.get("/api/v1/orchestrator/overrides", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["overrides"]["ic"]["fixed_cap_usd"] == 50000


# ── total-pool (T-20260516-105451 #3 capital-input-upfront) ────────────────
# Contract: ~/projects/cto/reviews/2026-05-16-total-pool-api-contract.md


def _total_pool_file(tmp_path: Path) -> Path:
    return tmp_path / ".nodeble-orchestrator" / "config" / "total_pool.json"


def test_total_pool_get_not_declared_when_absent(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.get("/api/v1/orchestrator/total-pool", headers=_hdr())
    assert r.status_code == 200  # declared:false is normal, not an error
    assert r.json() == {
        "declared": False,
        "total_pool_usd": None,
        "updated_at": None,
    }


def test_total_pool_get_declared_after_valid_write(client_with_fake_home):
    client, tmp_path = client_with_fake_home
    f = _total_pool_file(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "total_pool_usd": 250000,
        "updated_at": "2026-05-16T10:54:51-04:00",
        "source": "user_declared",
    }))
    r = client.get("/api/v1/orchestrator/total-pool", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {
        "declared": True,
        "total_pool_usd": 250000,
        "updated_at": "2026-05-16T10:54:51-04:00",
    }


def test_total_pool_get_not_declared_when_out_of_bounds(client_with_fake_home):
    # Gate + cron must agree on validity (§4): an out-of-bounds value
    # on disk reads as declared:false (same as orchestrator reader).
    client, tmp_path = client_with_fake_home
    f = _total_pool_file(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"total_pool_usd": 500, "updated_at": "x"}))  # <$1k
    r = client.get("/api/v1/orchestrator/total-pool", headers=_hdr())
    assert r.json()["declared"] is False


def test_total_pool_get_not_declared_when_corrupt(client_with_fake_home):
    client, tmp_path = client_with_fake_home
    f = _total_pool_file(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json")
    r = client.get("/api/v1/orchestrator/total-pool", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["declared"] is False


def test_total_pool_post_valid_writes_atomically_and_reallocates(
    client_with_fake_home, monkeypatch,
):
    client, tmp_path = client_with_fake_home

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(orch_route.subprocess, "run", fake_run)

    r = client.post(
        "/api/v1/orchestrator/total-pool",
        headers=_hdr(),
        json={"total_pool_usd": 250000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["total_pool_usd"] == 250000.0
    assert body["reallocate"] == "ok"
    assert body["updated_at"]  # ISO-8601 server now

    # File written with the contract schema (orchestrator parses this).
    on_disk = json.loads(_total_pool_file(tmp_path).read_text())
    assert on_disk["total_pool_usd"] == 250000.0
    assert on_disk["source"] == "user_declared"  # first set, no prior file
    assert on_disk["updated_at"] == body["updated_at"]
    # 0600 hygiene (matches overrides.yaml dir).
    assert oct(_total_pool_file(tmp_path).stat().st_mode)[-3:] == "600"


def test_total_pool_post_second_set_infers_settings_edit(
    client_with_fake_home, monkeypatch,
):
    client, tmp_path = client_with_fake_home
    monkeypatch.setattr(
        orch_route.subprocess, "run",
        lambda a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    client.post("/api/v1/orchestrator/total-pool", headers=_hdr(),
                json={"total_pool_usd": 100000})
    client.post("/api/v1/orchestrator/total-pool", headers=_hdr(),
                json={"total_pool_usd": 300000})
    on_disk = json.loads(_total_pool_file(tmp_path).read_text())
    assert on_disk["total_pool_usd"] == 300000.0
    assert on_disk["source"] == "settings_edit"  # file already existed


def test_total_pool_post_reallocate_deferred_on_subprocess_error(
    client_with_fake_home, monkeypatch,
):
    # Brand-fresh box: scores absent → allocate subprocess errors. The
    # SET must still 200 (reallocate:deferred), UI not blocked.
    client, tmp_path = client_with_fake_home

    def boom(args, **kw):
        raise OSError("orchestrator venv missing on fresh box")

    monkeypatch.setattr(orch_route.subprocess, "run", boom)
    r = client.post("/api/v1/orchestrator/total-pool", headers=_hdr(),
                     json={"total_pool_usd": 250000})
    assert r.status_code == 200
    assert r.json()["reallocate"] == "deferred"
    # The set itself still persisted.
    assert json.loads(_total_pool_file(tmp_path).read_text())[
        "total_pool_usd"] == 250000.0


def test_total_pool_post_reallocate_deferred_on_nonzero_rc(
    client_with_fake_home, monkeypatch,
):
    client, _ = client_with_fake_home
    monkeypatch.setattr(
        orch_route.subprocess, "run",
        lambda a, **k: subprocess.CompletedProcess(a, 3, stdout="", stderr="x"),
    )
    r = client.post("/api/v1/orchestrator/total-pool", headers=_hdr(),
                     json={"total_pool_usd": 250000})
    assert r.status_code == 200
    assert r.json()["reallocate"] == "deferred"  # nonzero rc → deferred, still 200


@pytest.mark.parametrize(
    "bad,expect_msg_contains",
    [
        (500, "金额太小"),                    # < $1k
        (0, "有效的美元金额"),                 # <= 0
        (-100, "有效的美元金额"),              # negative
        (2_000_000_000, "金额太大"),           # > $1B
        (True, "有效的美元金额"),              # bool is NOT a valid number
    ],
)
def test_total_pool_post_invalid_422_plain_language(
    client_with_fake_home, bad, expect_msg_contains,
):
    client, tmp_path = client_with_fake_home
    r = client.post("/api/v1/orchestrator/total-pool", headers=_hdr(),
                     json={"total_pool_usd": bad})
    assert r.status_code == 422
    assert expect_msg_contains in r.json()["detail"]
    # Invalid POST must NOT have written the file.
    assert not _total_pool_file(tmp_path).exists()


def test_total_pool_post_extra_field_forbidden_422(client_with_fake_home):
    client, _ = client_with_fake_home
    r = client.post("/api/v1/orchestrator/total-pool", headers=_hdr(),
                     json={"total_pool_usd": 250000, "typo": 1})
    assert r.status_code == 422  # extra='forbid'


def test_total_pool_requires_auth(client_with_fake_home):
    client, _ = client_with_fake_home
    assert client.get("/api/v1/orchestrator/total-pool").status_code == 401
    assert client.post("/api/v1/orchestrator/total-pool",
                        json={"total_pool_usd": 1}).status_code == 401


def test_total_pool_bounds_match_orchestrator_contract():
    """Contract §4 drift guard: api-server's replicated bounds MUST
    equal the orchestrator reader's single source of truth. If
    nodeble_orchestrator is co-installed, assert equality directly;
    else assert the documented constants (the contract doc + this
    test are the cross-repo pin when the import isn't available)."""
    assert orch_route.MIN_REASONABLE_POOL_USD == 1_000.0
    assert orch_route.MAX_REASONABLE_POOL_USD == 1_000_000_000.0
    try:
        from nodeble_orchestrator import capital_pool  # type: ignore
    except Exception:
        pytest.skip(
            "nodeble_orchestrator not co-installed (separate venv) — "
            "bounds pinned via contract doc §4 + the literals above"
        )
    assert (
        orch_route.MIN_REASONABLE_POOL_USD
        == capital_pool.MIN_REASONABLE_POOL_USD
    )
    assert (
        orch_route.MAX_REASONABLE_POOL_USD
        == capital_pool.MAX_REASONABLE_POOL_USD
    )

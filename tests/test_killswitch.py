"""Tests for the system killswitch endpoints + the editable-paths mode filter.

Mocks `run_shim` with an in-process function that actually rewrites the
yaml files — the killswitch logic needs to see real `old` / `new` values
come back from the shim, and the GET endpoint then re-reads the yaml for
aggregate state. This mirrors test_config_write's pattern.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import audit as audit_mod, config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.config_writer import ShimResult
from nodeble_api_server.routes import strategies as strategies_mod
from nodeble_api_server.routes import system as system_mod

VALID_TOKEN = "killswitch-test-token"


# ── Fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def client_and_home(tmp_path: Path, monkeypatch):
    """TestClient + tmp $HOME containing all 9 strategies' strategy.yaml.
    `run_shim` is patched on both route modules to rewrite the yaml
    in-place when action='set'."""
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

    # Build strategy.yaml for each of the 9 strategies, with mode=live.
    for sid, meta in state_reader.STRATEGY_REGISTRY.items():
        dir_ = tmp_path / meta["folder"] / "config"
        dir_.mkdir(parents=True)
        (dir_ / "strategy.yaml").write_text(
            yaml.safe_dump({"mode": "live", "selection": {"dte_ideal": 35}})
        )

    # Redirect Path.home() so _read_mode + state_reader land in tmp_path.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # Point venv_python at the current python so both routes' venv
    # existence checks pass. The strategies_mod copy is needed for
    # the editable-paths filter test.
    import sys
    real_python = Path(sys.executable)
    monkeypatch.setattr(
        system_mod,
        "strategy_venv_python",
        lambda sid, home=None: real_python,
    )
    monkeypatch.setattr(
        strategies_mod,
        "strategy_venv_python",
        lambda sid, home=None: real_python,
    )

    # Redirect the audit file to tmp so fsync doesn't touch ~/.
    audit_file = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setattr(audit_mod, "_DEFAULT_AUDIT_PATH", audit_file)

    # Fake run_shim: supports action="set" (rewrites yaml), action="list"
    # (returns a canned whitelist), action="validate" (no-op ok).
    def fake_run_shim(**kwargs):
        sid = kwargs["strategy_id"]
        action = kwargs["action"]
        meta = state_reader.STRATEGY_REGISTRY[sid]
        yaml_path = tmp_path / meta["folder"] / "config" / "strategy.yaml"

        if action == "list":
            # Group A and calendar already return "mode" in real life —
            # mimic that so the editable-paths filter test can verify it
            # gets stripped.
            return ShimResult(
                ok=True,
                old=None,
                new=["mode", "selection.dte_ideal"],
                error=None,
            )
        if action == "set":
            if kwargs["param_path"] != "mode":
                return ShimResult(ok=False, old=None, new=None, error="only mode supported in test")
            data = yaml.safe_load(yaml_path.read_text()) or {}
            old = data.get("mode")
            data["mode"] = kwargs["value"]
            yaml_path.write_text(yaml.safe_dump(data))
            return ShimResult(ok=True, old=old, new=kwargs["value"], error=None)
        if action == "validate":
            return ShimResult(ok=True, old=None, new=kwargs["value"], error=None)
        return ShimResult(ok=False, old=None, new=None, error=f"unknown action {action}")

    monkeypatch.setattr(system_mod, "run_shim", fake_run_shim)
    monkeypatch.setattr(strategies_mod, "run_shim", fake_run_shim)

    # Clear state_reader cache between tests so fresh yaml reads work.
    state_reader.clear_cache()

    return TestClient(app), tmp_path, audit_file


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def _read_audit(audit_file: Path) -> list[dict]:
    if not audit_file.exists():
        return []
    return [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]


# ── GET /killswitch ──────────────────────────────────────────────────────


def test_get_requires_auth(client_and_home):
    client, *_ = client_and_home
    r = client.get("/api/v1/system/killswitch")
    assert r.status_code == 401


def test_get_all_live_returns_disengaged(client_and_home):
    client, *_ = client_and_home
    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "disengaged"
    assert body["engaged"] is False
    assert all(v == "live" for v in body["per_strategy_mode"].values())


def test_get_all_dry_run_returns_engaged(client_and_home):
    client, tmp_path, _ = client_and_home
    # Flip every yaml to dry_run manually.
    for meta in state_reader.STRATEGY_REGISTRY.values():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        data = yaml.safe_load(p.read_text())
        data["mode"] = "dry_run"
        p.write_text(yaml.safe_dump(data))

    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["state"] == "engaged"
    assert body["engaged"] is True


def test_get_mixed_returns_partial(client_and_home):
    client, tmp_path, _ = client_and_home
    # Flip just one strategy.
    meta = state_reader.STRATEGY_REGISTRY["wheel"]
    p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
    data = yaml.safe_load(p.read_text())
    data["mode"] = "dry_run"
    p.write_text(yaml.safe_dump(data))

    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["state"] == "partial"
    assert body["engaged"] is False


def test_get_missing_yaml_mode_is_null(client_and_home):
    client, tmp_path, _ = client_and_home
    # Delete the mode field from one yaml.
    meta = state_reader.STRATEGY_REGISTRY["ic"]
    p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
    data = yaml.safe_load(p.read_text())
    del data["mode"]
    p.write_text(yaml.safe_dump(data))

    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["per_strategy_mode"]["ic"] is None
    # Other 8 are live — aggregate is still "disengaged".
    assert body["state"] == "disengaged"


# ── POST /killswitch ─────────────────────────────────────────────────────


def test_post_engage_flips_all_strategies(client_and_home):
    client, tmp_path, audit_file = client_and_home
    r = client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "market volatility"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "engaged"
    assert body["engaged"] is True
    # Every strategy changed from live -> dry_run.
    for sid, entry in body["result"].items():
        assert entry["ok"] is True, f"{sid}: {entry}"
        assert entry["old_mode"] == "live"
        assert entry["new_mode"] == "dry_run"
        assert entry["changed"] is True

    # YAML files on disk were rewritten.
    for meta in state_reader.STRATEGY_REGISTRY.values():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "dry_run"

    # Audit: 9 per-strategy entries + 1 system entry = 10.
    entries = _read_audit(audit_file)
    assert len(entries) == 10
    sys_entries = [e for e in entries if e["strategy"] == "system"]
    assert len(sys_entries) == 1
    sys_entry = sys_entries[0]
    assert sys_entry["param_path"] == "killswitch"
    assert sys_entry["old_value"] == "disengaged"
    assert sys_entry["new_value"] == "engaged"
    assert sys_entry["result"] == "success"
    assert sys_entry["reason"] == "market volatility"


def test_post_disengage_flips_back(client_and_home):
    client, tmp_path, _ = client_and_home
    # First engage.
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True},
    )
    # Then disengage.
    r = client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": False, "reason": "back to live"},
    )
    body = r.json()
    assert body["state"] == "disengaged"
    assert body["engaged"] is False
    for meta in state_reader.STRATEGY_REGISTRY.values():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "live"


def test_post_engage_is_idempotent(client_and_home):
    client, _, audit_file = client_and_home
    # First engage.
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True},
    )
    entries_after_first = _read_audit(audit_file)

    # Engage again (all already dry_run).
    r = client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "engaged"
    # No strategy "changed" the second time.
    for sid, entry in body["result"].items():
        assert entry["changed"] is False, f"{sid} unexpectedly changed"

    # Only the system-level noop audit entry added (no per-strategy).
    entries_after_second = _read_audit(audit_file)
    added = entries_after_second[len(entries_after_first):]
    assert len(added) == 1
    assert added[0]["strategy"] == "system"
    assert added[0]["result"] == "noop"
    # Old and new aggregate state both 'engaged' on noop.
    assert added[0]["old_value"] == "engaged"
    assert added[0]["new_value"] == "engaged"


def test_post_partial_failure_reports_partial_state(
    client_and_home, monkeypatch
):
    client, tmp_path, audit_file = client_and_home

    # Replace run_shim with one that fails for wheel only.
    def partial_failing_shim(**kwargs):
        sid = kwargs["strategy_id"]
        action = kwargs["action"]
        if action == "set" and sid == "wheel":
            return ShimResult(
                ok=False, old="live", new=None, error="simulated timeout"
            )
        # Fall back to the real flip for other strategies.
        if action == "set":
            meta = state_reader.STRATEGY_REGISTRY[sid]
            p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
            data = yaml.safe_load(p.read_text()) or {}
            old = data.get("mode")
            data["mode"] = kwargs["value"]
            p.write_text(yaml.safe_dump(data))
            return ShimResult(ok=True, old=old, new=kwargs["value"], error=None)
        return ShimResult(ok=True, old=None, new=None, error=None)

    monkeypatch.setattr(system_mod, "run_shim", partial_failing_shim)

    r = client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "partial test"},
    )
    body = r.json()
    assert body["state"] == "partial"  # wheel still live, others dry_run
    assert body["engaged"] is False    # engaged requires ALL dry_run
    assert body["result"]["wheel"]["ok"] is False
    assert "timeout" in body["result"]["wheel"]["error"].lower() or \
           "timed out" in body["result"]["wheel"]["error"].lower() or \
           body["result"]["wheel"]["error"] == "simulated timeout"
    # 8 other strategies succeeded.
    ok_count = sum(1 for r in body["result"].values() if r["ok"])
    assert ok_count == 8

    sys_entries = [e for e in _read_audit(audit_file) if e["strategy"] == "system"]
    assert sys_entries[-1]["result"] == "partial"
    assert sys_entries[-1]["new_value"] == "partial"


def test_post_reason_max_length_enforced(client_and_home):
    client, *_ = client_and_home
    # Over 500 chars should 422.
    r = client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "x" * 501},
    )
    assert r.status_code == 422


def test_post_requires_auth(client_and_home):
    client, *_ = client_and_home
    r = client.post("/api/v1/system/killswitch", json={"engaged": True})
    assert r.status_code == 401


# ── GET history fields ──────────────────────────────────────────────────


def test_get_surfaces_engaged_at_from_audit(client_and_home):
    client, _, audit_file = client_and_home
    # Engage, then GET.
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "test engage"},
    )
    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["engaged_at"] is not None
    # engaged_at is the ts of the most recent system audit entry.
    entries = _read_audit(audit_file)
    sys_ts = [e["ts"] for e in entries if e["strategy"] == "system"][-1]
    assert body["engaged_at"] == sys_ts
    assert body["last_change_reason"] == "test engage"


# ── editable-paths filter ────────────────────────────────────────────────


def test_editable_paths_filters_mode_field(client_and_home):
    """The shim's `list` action returns "mode" in our fake, but the route
    must strip it so the Config tab doesn't expose a ✎ on mode."""
    client, *_ = client_and_home
    r = client.get(
        "/api/v1/strategies/ic/config/editable-paths",
        headers=_hdr(),
    )
    assert r.status_code == 200
    paths = r.json()["editable_paths"]
    assert "mode" not in paths
    assert "selection.dte_ideal" in paths  # other paths still surface

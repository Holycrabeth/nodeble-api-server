"""Tests for the system killswitch endpoints + the editable-paths mode filter.

Mocks `run_shim` with an in-process function that actually rewrites the
yaml files — the killswitch logic needs to see real `old` / `new` values
come back from the shim, and the GET endpoint then re-reads the yaml for
aggregate state. This mirrors test_config_write's pattern.
"""
from __future__ import annotations

import json
import logging
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


def test_get_all_dry_run_after_engage_returns_engaged(client_and_home):
    """After operator POSTs engage, state must be 'engaged' AND
    engaged=true. We press via the API (not by flipping yaml directly)
    so the intent audit entry is written — engaged=true is an
    OPERATOR-intent flag, not a mode-aggregate derivation."""
    client, *_ = client_and_home
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True},
    )
    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["state"] == "engaged"
    assert body["engaged"] is True


def test_get_out_of_band_dry_run_without_engage_stays_disengaged(
    client_and_home,
):
    """Someone hand-edits every strategy.yaml to dry_run without going
    through the killswitch endpoint. Ground-truth state is 'engaged',
    but operator intent is still disengaged — engaged=false."""
    client, tmp_path, _ = client_and_home
    for meta in state_reader.STRATEGY_REGISTRY.values():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        data = yaml.safe_load(p.read_text())
        data["mode"] = "dry_run"
        p.write_text(yaml.safe_dump(data))

    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["state"] == "engaged"
    assert body["engaged"] is False  # no audit, no operator intent


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
    # engaged=false even though state=partial, because no operator ever
    # pressed the killswitch (baseline mixed state is normal for the 9
    # strategies — Calendar etc. default to dry_run out of the box).
    assert body["engaged"] is False


def test_get_engaged_reflects_intent_not_aggregate(client_and_home):
    """The critical UX guarantee — baseline mixed state must not flag
    the TopBar as 'paused' on first app launch."""
    client, tmp_path, _ = client_and_home
    # Baseline-like: 4 live + 5 dry_run (matching real Tower baseline).
    for sid in ("calendar", "collar", "ironbutterfly", "straddle", "strangle"):
        meta = state_reader.STRATEGY_REGISTRY[sid]
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        data = yaml.safe_load(p.read_text())
        data["mode"] = "dry_run"
        p.write_text(yaml.safe_dump(data))

    r = client.get("/api/v1/system/killswitch", headers=_hdr())
    body = r.json()
    assert body["state"] == "partial"
    # No audit → operator hasn't pressed anything → engaged must be false
    # regardless of how per-strategy modes happen to sit.
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
    # engaged reflects operator INTENT: they pressed engage, so engaged=true
    # even though the fleet ended up partial. UI maps (engaged=true,
    # state=partial) to the 🟡 "partial" button state with a retry CTA.
    assert body["engaged"] is True
    assert body["result"]["wheel"]["ok"] is False
    assert "timeout" in body["result"]["wheel"]["error"].lower() or \
           "timed out" in body["result"]["wheel"]["error"].lower() or \
           body["result"]["wheel"]["error"] == "simulated timeout"
    # 8 other strategies succeeded.
    ok_count = sum(1 for r in body["result"].values() if r["ok"])
    assert ok_count == 8

    sys_entries = [e for e in _read_audit(audit_file) if e["strategy"] == "system"]
    assert sys_entries[-1]["result"] == "partial"
    # new_value is intent ("engaged") — not the aggregate state.
    assert sys_entries[-1]["new_value"] == "engaged"


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


# ── Pre-engage snapshot lifecycle (per-strategy restore on disengage) ────


def _set_yaml_mode(tmp_path: Path, sid: str, mode: str) -> None:
    """Helper: directly write a strategy's yaml mode (no API call)."""
    meta = state_reader.STRATEGY_REGISTRY[sid]
    p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
    data = yaml.safe_load(p.read_text()) or {}
    data["mode"] = mode
    p.write_text(yaml.safe_dump(data))


def _set_baseline_mixed(tmp_path: Path) -> None:
    """Seed a realistic Tower-style baseline: IC / Wheel / PMCC / DS live,
    rest dry_run. Exercises the scenario the snapshot feature exists for."""
    for sid in ("calendar", "collar", "ironbutterfly", "straddle", "strangle"):
        _set_yaml_mode(tmp_path, sid, "dry_run")
    # The fixture already seeds all as "live", so only the 5 above need flipping.


def _snapshot_file_path(tmp_path: Path) -> Path:
    return tmp_path / ".nodeble-api" / "killswitch" / "pre_engage.json"


def test_engage_writes_pre_engage_snapshot(client_and_home):
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)

    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "first engage"},
    )
    snap = _snapshot_file_path(tmp_path)
    assert snap.exists(), "snapshot file should be written on first engage"
    data = json.loads(snap.read_text())
    assert data["per_strategy_mode"]["ic"] == "live"
    assert data["per_strategy_mode"]["calendar"] == "dry_run"
    assert data["per_strategy_mode"]["strangle"] == "dry_run"
    assert data["captured_reason"] == "first engage"
    assert "captured_at" in data


def test_engage_twice_preserves_earliest_snapshot(client_and_home):
    """Re-engage while already engaged must NOT overwrite the snapshot.
    The earliest-baseline-wins rule is what makes the restore
    semantically consistent across idempotent presses."""
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)

    # First engage — captures the mixed baseline.
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "first"},
    )
    snap_before = json.loads(_snapshot_file_path(tmp_path).read_text())
    captured_at_first = snap_before["captured_at"]

    # Corrupt the snapshot intent: manually flip a strategy AFTER engage.
    # Then re-engage — snapshot must still hold the ORIGINAL pre-engage
    # modes, not the post-flip state.
    _set_yaml_mode(tmp_path, "ic", "live")  # hypothetically reverted
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "second"},
    )
    snap_after = json.loads(_snapshot_file_path(tmp_path).read_text())
    # Both timestamp and reason should be unchanged.
    assert snap_after["captured_at"] == captured_at_first
    assert snap_after["captured_reason"] == "first"


def test_disengage_restores_per_snapshot_not_all_live(client_and_home):
    """The core UX fix: disengage must return each strategy to its
    PRE-ENGAGE mode, not a blanket live. Otherwise the 5 baseline-
    dry_run strategies get silently flipped to real trading."""
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)

    # Full cycle: engage then disengage.
    client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": True, "reason": "test engage"},
    )
    # Sanity: after engage, all 9 are dry_run.
    for sid, meta in state_reader.STRATEGY_REGISTRY.items():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "dry_run"

    r = client.post(
        "/api/v1/system/killswitch",
        headers=_hdr(),
        json={"engaged": False, "reason": "test disengage"},
    )
    body = r.json()
    assert body["engaged"] is False

    # Post-disengage must equal pre-engage baseline, not all live.
    for sid in ("ic", "wheel", "pmcc", "directionalspread"):
        meta = state_reader.STRATEGY_REGISTRY[sid]
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "live", f"{sid} should be live"
    for sid in ("calendar", "collar", "ironbutterfly", "straddle", "strangle"):
        meta = state_reader.STRATEGY_REGISTRY[sid]
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "dry_run", (
            f"{sid} should be RESTORED to dry_run, not flipped to live"
        )


def test_disengage_deletes_snapshot_on_full_success(client_and_home):
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)

    client.post(
        "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": True},
    )
    assert _snapshot_file_path(tmp_path).exists()

    client.post(
        "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": False},
    )
    assert not _snapshot_file_path(tmp_path).exists(), (
        "full-success disengage should clean up the snapshot"
    )


def test_disengage_preserves_snapshot_on_partial_failure(
    client_and_home, monkeypatch
):
    """If one strategy's shim errors during disengage, keep the snapshot
    so a retry can restore the remaining strategies from the same
    baseline."""
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)

    # First engage succeeds with the normal fake shim.
    client.post(
        "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": True},
    )
    assert _snapshot_file_path(tmp_path).exists()

    # Replace the shim with one that fails wheel's restore.
    def partial_failing_shim(**kwargs):
        if kwargs["action"] == "set" and kwargs["strategy_id"] == "wheel":
            return ShimResult(
                ok=False, old="dry_run", new=None, error="simulated timeout"
            )
        if kwargs["action"] == "set":
            sid = kwargs["strategy_id"]
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
        "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": False},
    )
    assert r.json()["result"]["wheel"]["ok"] is False
    assert _snapshot_file_path(tmp_path).exists(), (
        "partial-failure disengage should keep the snapshot for retry"
    )


def test_disengage_without_snapshot_falls_back_to_all_live(
    client_and_home, caplog
):
    """curl/hand-triggered disengage with no prior engage — snapshot
    missing → fall back to the pre-snapshot flip-all-to-live behavior +
    emit a WARNING."""
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)
    # No engage first.
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.routes.system"):
        client.post(
            "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": False},
        )
    # All yaml should be live (old behavior without snapshot).
    for meta in state_reader.STRATEGY_REGISTRY.values():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "live"
    # Warning emitted.
    assert any(
        "no pre-engage snapshot" in r.getMessage() for r in caplog.records
    )


def test_disengage_with_corrupt_snapshot_falls_back(client_and_home, caplog):
    """Malformed snapshot JSON → treated the same as missing. Must not
    crash the endpoint."""
    client, tmp_path, _ = client_and_home
    _set_baseline_mixed(tmp_path)
    # Write garbage directly to the snapshot path.
    snap = _snapshot_file_path(tmp_path)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text("{not: valid: json,,")

    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.routes.system"):
        r = client.post(
            "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": False},
        )
    assert r.status_code == 200
    # Fell back to all-live.
    for meta in state_reader.STRATEGY_REGISTRY.values():
        p = tmp_path / meta["folder"] / "config" / "strategy.yaml"
        assert yaml.safe_load(p.read_text())["mode"] == "live"


def test_disengage_strategy_not_in_snapshot_defaults_to_live(
    client_and_home, tmp_path
):
    """Edge: snapshot was taken with N strategies, but STRATEGY_REGISTRY
    has gained a new one since. The new strategy isn't in the snapshot
    → must default to 'live' (not crash, not leave it in whatever
    unintended state)."""
    client, app_tmp, _ = client_and_home
    _set_baseline_mixed(app_tmp)
    # Engage normally.
    client.post(
        "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": True},
    )
    # Surgically remove one strategy from the snapshot to simulate it
    # having been added to the registry after engage.
    snap_path = _snapshot_file_path(app_tmp)
    data = json.loads(snap_path.read_text())
    del data["per_strategy_mode"]["ironbutterfly"]
    snap_path.write_text(json.dumps(data))

    client.post(
        "/api/v1/system/killswitch", headers=_hdr(), json={"engaged": False},
    )
    meta = state_reader.STRATEGY_REGISTRY["ironbutterfly"]
    p = app_tmp / meta["folder"] / "config" / "strategy.yaml"
    # IronButterfly has no snapshot entry → default "live".
    assert yaml.safe_load(p.read_text())["mode"] == "live"

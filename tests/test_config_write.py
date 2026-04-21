"""Integration test for the commit_config cache-invalidation path.

M1.h polish: after a successful PUT /config, the state_reader 5s cache
must be cleared immediately so the next GET /strategies/{id} (triggered
by the frontend's invalidateQueries) returns the new YAML value, not
the stale pre-write cache.

This test mocks the shim subprocess (no real strategy venv required)
and checks both the direct contract (read_config sees the new value
without waiting 5s) and the wiring (clear_cache is invoked inside the
commit path).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.config_writer import ShimResult
from nodeble_api_server.routes import strategies as routes_mod

VALID_TOKEN = "config-write-test-token"


@pytest.fixture
def client_and_paths(tmp_path: Path, monkeypatch):
    """Fake $HOME at tmp_path; real venv pointer at a path that exists
    (python itself); mocked run_shim that actually rewrites the YAML on
    the 'set' action so the cache-invalidation test has something real
    to compare against."""
    # API config: one token.
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [{"token": VALID_TOKEN, "label": "test"}],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    # Fake strategy dir.
    strategy_dir = tmp_path / ".nodeble"
    (strategy_dir / "config").mkdir(parents=True)
    yaml_path = strategy_dir / "config" / "strategy.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "mode": "live",
                "selection": {"put_delta_max": 0.22, "dte_ideal": 35},
                "management": {"stop_loss_pct": 5.0},
            }
        )
    )

    # Route's _resolve_shim checks the venv path exists — point it at
    # the Python we're running under.
    import sys
    real_python = Path(sys.executable)
    monkeypatch.setattr(
        routes_mod,
        "strategy_venv_python",
        lambda sid, home=None: real_python,
    )

    # state_reader uses Path.home() internally — redirect it.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # Audit log: tmp path so fsync doesn't touch ~/.nodeble-api/.
    audit_dir = tmp_path / "audit"
    from nodeble_api_server import audit as audit_mod
    monkeypatch.setattr(
        audit_mod, "_DEFAULT_AUDIT_PATH", audit_dir / "audit.jsonl"
    )

    return TestClient(app), yaml_path


def test_commit_config_clears_state_reader_cache(
    client_and_paths, monkeypatch
):
    """The bug we're fixing: before the fix, read_config would return
    stale data for up to 5s after a write because the cache wasn't
    invalidated. After the fix, it returns fresh immediately."""
    client, yaml_path = client_and_paths

    # Prime the cache by reading the config once. State_reader caches
    # with a 5s TTL; a second read within the TTL hits the cache.
    before = state_reader.read_config("ic")
    assert before is not None
    assert before["selection"]["put_delta_max"] == 0.22

    # Fake shim: validate always ok; set writes the new YAML ourselves
    # (simulating what the real shim subprocess would do).
    def fake_run_shim(**kwargs):
        if kwargs["action"] == "validate":
            return ShimResult(ok=True, old=0.22, new=0.20, error=None)
        # action == "set"
        data = yaml.safe_load(yaml_path.read_text())
        data["selection"]["put_delta_max"] = 0.20
        yaml_path.write_text(yaml.safe_dump(data))
        return ShimResult(ok=True, old=0.22, new=0.20, error=None)

    monkeypatch.setattr(routes_mod, "run_shim", fake_run_shim)

    r = client.put(
        "/api/v1/strategies/ic/config",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        json={
            "param_path": "selection.put_delta_max",
            "new_value": 0.20,
            "reason": "cache invalidation test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["committed"] is True
    assert body["new_value"] == 0.20

    # The key assertion: an immediate re-read returns the NEW value.
    # Without clear_cache() inside commit_config, this would still be
    # 0.22 from the primed cache for up to 5 seconds.
    after = state_reader.read_config("ic")
    assert after["selection"]["put_delta_max"] == 0.20


def test_commit_config_does_not_clear_cache_on_validation_failure(
    client_and_paths, monkeypatch
):
    """If validation fails mid-commit, don't bother clearing cache —
    YAML wasn't touched. This test locks that behavior so a future
    refactor doesn't accidentally churn the cache on every 4xx."""
    client, _ = client_and_paths

    spy = {"cleared": 0}
    real_clear = state_reader.clear_cache

    def spy_clear():
        spy["cleared"] += 1
        real_clear()

    monkeypatch.setattr(routes_mod, "clear_cache", spy_clear)

    def fake_run_shim(**kwargs):
        return ShimResult(
            ok=False,
            old=0.22,
            new=None,
            error="value out of range",
        )

    monkeypatch.setattr(routes_mod, "run_shim", fake_run_shim)

    r = client.put(
        "/api/v1/strategies/ic/config",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        json={
            "param_path": "selection.put_delta_max",
            "new_value": 99.0,
            "reason": "",
        },
    )
    assert r.status_code == 400
    assert spy["cleared"] == 0


def test_clear_cache_is_actually_effective():
    """Sanity: verify state_reader.clear_cache wipes the cache dict."""
    # Populate directly.
    state_reader._cache[("test", "x", "y")] = ("something", 0.0)
    assert ("test", "x", "y") in state_reader._cache
    state_reader.clear_cache()
    assert state_reader._cache == {}

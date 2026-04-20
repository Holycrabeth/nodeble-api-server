"""Unit tests for snapshot.build_* helpers."""
from pathlib import Path

import pytest

from nodeble_api_server import __version__
from nodeble_api_server import snapshot, state_reader


def test_build_server_info_shape():
    info = snapshot.build_server_info()
    assert info["version"] == __version__
    assert info["api_version"] == "v1"
    assert isinstance(info["hostname"], str) and len(info["hostname"]) > 0
    assert isinstance(info["uptime_sec"], int) and info["uptime_sec"] >= 0


def test_build_strategies_list_empty_home(monkeypatch, tmp_path):
    """With an empty fake $HOME, no strategies are installed → empty list."""
    state_reader.clear_cache()

    def fake_list(home: Path | None = None):
        return []

    monkeypatch.setattr(state_reader, "list_installed_strategies", fake_list)
    # build_strategies_list imports list_installed_strategies from state_reader
    # at call time via `from ... import`, so we need to patch the binding in
    # snapshot's namespace too.
    monkeypatch.setattr(snapshot, "list_installed_strategies", fake_list)

    assert snapshot.build_strategies_list() == []


def test_build_snapshot_has_both_keys(monkeypatch):
    monkeypatch.setattr(snapshot, "list_installed_strategies", lambda: [])
    out = snapshot.build_snapshot()
    assert "strategies" in out
    assert "server_info" in out
    assert out["strategies"] == []
    assert out["server_info"]["api_version"] == "v1"


def test_build_strategy_card_unknown_id_raises(monkeypatch):
    # Implementation reads STRATEGY_REGISTRY[strategy_id] — unknown id KeyErrors.
    # We don't claim a user-friendly failure mode for internal callers.
    with pytest.raises(KeyError):
        state_reader.build_strategy_card("nonexistent-strategy")

"""Unit tests for shim helpers. The subprocess-driven shim scripts
themselves are smoke-tested live against Tower's real configs; here we
cover the pure-logic bits (`_yaml_helpers`, `_whitelist_shim` driver)
with direct imports so no strategy venv is required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodeble_api_server.shims._yaml_helpers import (
    atomic_write_yaml,
    get_by_path,
    parse_value,
    read_yaml,
    set_by_path,
    validate_bounds,
)


# ── parse_value ────────────────────────────────────────────────────────────


def test_parse_int_accepts_int_string():
    assert parse_value("35", "int") == 35


def test_parse_int_rejects_float_string():
    with pytest.raises(ValueError):
        parse_value("35.5", "int")


def test_parse_float():
    assert parse_value("0.22", "float") == 0.22
    assert parse_value("5", "float") == 5.0


def test_parse_bool_truthy():
    assert parse_value("true", "bool") is True
    assert parse_value("YES", "bool") is True
    assert parse_value("1", "bool") is True


def test_parse_bool_falsy():
    assert parse_value("false", "bool") is False
    assert parse_value("no", "bool") is False
    assert parse_value("0", "bool") is False


def test_parse_bool_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_value("maybe", "bool")


def test_parse_str_passthrough():
    assert parse_value("live", "str") == "live"


def test_parse_unknown_type_raises():
    with pytest.raises(ValueError):
        parse_value("x", "complex")


# ── validate_bounds ────────────────────────────────────────────────────────


def test_bounds_min_max_pass():
    defn = {"min": 0, "max": 10}
    assert validate_bounds(5, defn) is None


def test_bounds_below_min():
    defn = {"min": 0, "max": 10}
    assert validate_bounds(-1, defn) is not None


def test_bounds_above_max():
    defn = {"min": 0, "max": 10}
    assert validate_bounds(11, defn) is not None


def test_bounds_choices_pass():
    defn = {"choices": ["live", "dry_run"]}
    assert validate_bounds("live", defn) is None


def test_bounds_choices_reject():
    defn = {"choices": ["live", "dry_run"]}
    err = validate_bounds("production", defn)
    assert err is not None
    assert "dry_run" in err


def test_bounds_no_constraint_passes():
    """When defn has no min/max/choices, anything passes."""
    assert validate_bounds(42, {}) is None
    assert validate_bounds("anything", {}) is None


# ── get_by_path / set_by_path ──────────────────────────────────────────────


def test_get_by_path_single_key():
    assert get_by_path({"mode": "live"}, "mode") == "live"


def test_get_by_path_dotted():
    data = {"selection": {"dte_min": 30, "nested": {"deep": "ok"}}}
    assert get_by_path(data, "selection.dte_min") == 30
    assert get_by_path(data, "selection.nested.deep") == "ok"


def test_get_by_path_missing_returns_none():
    assert get_by_path({}, "selection.dte_min") is None
    assert get_by_path({"selection": "scalar"}, "selection.x") is None


def test_set_by_path_creates_intermediate_dicts():
    data = {}
    set_by_path(data, "a.b.c", 1)
    assert data == {"a": {"b": {"c": 1}}}


def test_set_by_path_overwrites_non_dict_intermediate():
    """If a.b existed as a scalar, set_by_path replaces it with a dict."""
    data = {"a": {"b": "scalar"}}
    set_by_path(data, "a.b.c", 1)
    assert data == {"a": {"b": {"c": 1}}}


def test_set_by_path_preserves_siblings():
    data = {"selection": {"dte_min": 30, "dte_max": 45}}
    set_by_path(data, "selection.dte_min", 35)
    assert data == {"selection": {"dte_min": 35, "dte_max": 45}}


# ── atomic_write_yaml ──────────────────────────────────────────────────────


def test_atomic_write_yaml_roundtrip(tmp_path: Path):
    path = tmp_path / "cfg.yaml"
    data = {"mode": "live", "selection": {"dte_min": 30}}
    atomic_write_yaml(path, data)
    assert path.exists()
    loaded = read_yaml(path)
    assert loaded == data


def test_atomic_write_yaml_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "sub" / "cfg.yaml"
    atomic_write_yaml(path, {"x": 1})
    assert path.exists()


def test_atomic_write_leaves_no_tmp_on_success(tmp_path: Path):
    path = tmp_path / "cfg.yaml"
    atomic_write_yaml(path, {"x": 1})
    tmps = [p for p in tmp_path.iterdir() if p.name.endswith(".yaml") and p.name != "cfg.yaml"]
    assert tmps == []


def test_read_yaml_missing_returns_empty():
    assert read_yaml(Path("/nonexistent/path.yaml")) == {}


def test_read_yaml_empty_file(tmp_path: Path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert read_yaml(path) == {}


def test_read_yaml_non_dict_returns_empty(tmp_path: Path):
    """Safety: if a YAML file contains just a list or scalar at root,
    return {} rather than leak the wrong shape."""
    path = tmp_path / "list.yaml"
    path.write_text("- one\n- two")
    assert read_yaml(path) == {}


# ── End-to-end whitelist shim test (in-process) ────────────────────────────


def test_whitelist_shim_validate_ok(tmp_path, monkeypatch, capsys):
    """Drive _whitelist_shim.run_shim directly (no subprocess) against a
    temp strategy directory."""
    from nodeble_api_server.shims import _whitelist_shim

    strategy_dir = tmp_path / ".nodeble-strangle"
    (strategy_dir / "config").mkdir(parents=True)
    yaml_path = strategy_dir / "config" / "strategy.yaml"
    yaml_path.write_text("selection:\n  delta_min: 0.05\n")

    whitelist = {
        "selection.delta_min": {"type": "float", "min": 0.01, "max": 0.5},
    }

    monkeypatch.setattr("sys.argv", ["prog", "validate", "strangle", "selection.delta_min", "0.15"])
    with pytest.raises(SystemExit):
        _whitelist_shim.run_shim("strangle", strategy_dir, whitelist)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload == {"ok": True, "old": 0.05, "new": 0.15, "error": None}


def test_whitelist_shim_validate_out_of_bounds(tmp_path, monkeypatch, capsys):
    from nodeble_api_server.shims import _whitelist_shim

    strategy_dir = tmp_path / ".nodeble-strangle"
    (strategy_dir / "config").mkdir(parents=True)
    (strategy_dir / "config" / "strategy.yaml").write_text("selection:\n  delta_min: 0.05\n")

    whitelist = {"selection.delta_min": {"type": "float", "min": 0.01, "max": 0.5}}

    monkeypatch.setattr("sys.argv", ["prog", "validate", "strangle", "selection.delta_min", "1.5"])
    with pytest.raises(SystemExit):
        _whitelist_shim.run_shim("strangle", strategy_dir, whitelist)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "above max" in payload["error"]


def test_whitelist_shim_set_writes_atomically(tmp_path, monkeypatch, capsys):
    from nodeble_api_server.shims import _whitelist_shim

    strategy_dir = tmp_path / ".nodeble-strangle"
    (strategy_dir / "config").mkdir(parents=True)
    yaml_path = strategy_dir / "config" / "strategy.yaml"
    yaml_path.write_text("selection:\n  delta_min: 0.05\n  dte_min: 30\n")

    whitelist = {"selection.delta_min": {"type": "float", "min": 0.01, "max": 0.5}}

    monkeypatch.setattr("sys.argv", ["prog", "set", "strangle", "selection.delta_min", "0.15"])
    with pytest.raises(SystemExit):
        _whitelist_shim.run_shim("strangle", strategy_dir, whitelist)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "old": 0.05, "new": 0.15, "error": None}

    # File was updated; sibling key preserved.
    reloaded = read_yaml(yaml_path)
    assert reloaded == {"selection": {"delta_min": 0.15, "dte_min": 30}}


def test_whitelist_shim_rejects_unknown_path(tmp_path, monkeypatch, capsys):
    from nodeble_api_server.shims import _whitelist_shim

    strategy_dir = tmp_path / ".nodeble-strangle"
    (strategy_dir / "config").mkdir(parents=True)
    (strategy_dir / "config" / "strategy.yaml").write_text("selection:\n  delta_min: 0.05\n")

    monkeypatch.setattr("sys.argv", ["prog", "validate", "strangle", "selection.not_a_param", "0.1"])
    with pytest.raises(SystemExit):
        _whitelist_shim.run_shim("strangle", strategy_dir, {"selection.delta_min": {"type": "float"}})
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "whitelist" in payload["error"]

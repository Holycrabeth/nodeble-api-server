"""Tests for config_writer.run_shim — the subprocess layer.

We avoid depending on any strategy's real venv by pointing at our own
Python and passing a sentinel shim module that doesn't exist, OR by
writing a minimal shim-stub on disk and invoking it.

The critical behaviors to pin down are: JSON parsing, timeout → SIGKILL,
crashed subprocess → error surface, no shim output → error surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from nodeble_api_server.config_writer import run_shim


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_run_shim_parses_json_success(tmp_path: Path, monkeypatch):
    """Stub shim that emits a valid success payload."""
    stub_dir = tmp_path / "stub_pkg"
    _write_stub(stub_dir / "__init__.py", "")
    _write_stub(
        stub_dir / "ok.py",
        'import sys, json; sys.stdout.write(json.dumps({"ok": True, "old": 1, "new": 2, "error": None}) + "\\n")',
    )

    monkeypatch.setattr(
        "nodeble_api_server.config_writer._shim_module",
        lambda name: f"stub_pkg.{name}",
    )
    res = run_shim(
        venv_python=Path(sys.executable),
        shim_name="ok",
        action="set",
        strategy_id="x",
        param_path="a.b",
        value=2,
        api_server_src=tmp_path,
    )
    assert res.ok is True
    assert res.old == 1
    assert res.new == 2
    assert res.error is None


def test_run_shim_parses_json_failure(tmp_path: Path, monkeypatch):
    stub_dir = tmp_path / "stub_pkg"
    _write_stub(stub_dir / "__init__.py", "")
    _write_stub(
        stub_dir / "fail.py",
        'import sys, json; sys.stdout.write(json.dumps({"ok": False, "old": None, "new": None, "error": "bad value"}) + "\\n")',
    )

    monkeypatch.setattr(
        "nodeble_api_server.config_writer._shim_module",
        lambda name: f"stub_pkg.{name}",
    )
    res = run_shim(
        venv_python=Path(sys.executable),
        shim_name="fail",
        action="validate",
        strategy_id="x",
        param_path="a",
        value=1,
        api_server_src=tmp_path,
    )
    assert res.ok is False
    assert res.error == "bad value"


def test_run_shim_timeout_reports_timeout(tmp_path: Path, monkeypatch):
    """Shim that sleeps past timeout → SIGKILL + timeout message."""
    stub_dir = tmp_path / "stub_pkg"
    _write_stub(stub_dir / "__init__.py", "")
    _write_stub(stub_dir / "slow.py", "import time; time.sleep(5)")

    monkeypatch.setattr(
        "nodeble_api_server.config_writer._shim_module",
        lambda name: f"stub_pkg.{name}",
    )
    res = run_shim(
        venv_python=Path(sys.executable),
        shim_name="slow",
        action="set",
        strategy_id="x",
        param_path="a",
        value=1,
        timeout_sec=0.3,
        api_server_src=tmp_path,
    )
    assert res.ok is False
    assert res.error is not None
    assert "timed out" in res.error


def test_run_shim_crashed_before_output(tmp_path: Path, monkeypatch):
    stub_dir = tmp_path / "stub_pkg"
    _write_stub(stub_dir / "__init__.py", "")
    _write_stub(stub_dir / "boom.py", 'raise RuntimeError("synthetic crash")')

    monkeypatch.setattr(
        "nodeble_api_server.config_writer._shim_module",
        lambda name: f"stub_pkg.{name}",
    )
    res = run_shim(
        venv_python=Path(sys.executable),
        shim_name="boom",
        action="validate",
        strategy_id="x",
        param_path="a",
        value=1,
        api_server_src=tmp_path,
    )
    assert res.ok is False
    assert res.error is not None
    assert "crashed" in res.error or "synthetic" in res.error


def test_run_shim_non_json_stdout(tmp_path: Path, monkeypatch):
    stub_dir = tmp_path / "stub_pkg"
    _write_stub(stub_dir / "__init__.py", "")
    _write_stub(stub_dir / "garbled.py", "print('not JSON at all')")

    monkeypatch.setattr(
        "nodeble_api_server.config_writer._shim_module",
        lambda name: f"stub_pkg.{name}",
    )
    res = run_shim(
        venv_python=Path(sys.executable),
        shim_name="garbled",
        action="validate",
        strategy_id="x",
        param_path="a",
        value=1,
        api_server_src=tmp_path,
    )
    assert res.ok is False
    assert res.error is not None
    assert "not JSON" in res.error or "JSON" in res.error


def test_run_shim_value_json_encoded_correctly(tmp_path: Path, monkeypatch):
    """Verify the `value` arg arrives at the shim as JSON-encoded — string
    vs number vs bool all preserved."""
    stub_dir = tmp_path / "stub_pkg"
    _write_stub(stub_dir / "__init__.py", "")
    _write_stub(
        stub_dir / "echo.py",
        'import sys, json\n'
        'value_json = sys.argv[4]\n'
        'sys.stdout.write(json.dumps({"ok": True, "old": None, "new": json.loads(value_json), "error": None}) + "\\n")',
    )

    monkeypatch.setattr(
        "nodeble_api_server.config_writer._shim_module",
        lambda name: f"stub_pkg.{name}",
    )

    for value in [0.20, 35, True, False, "live", [1, 2, 3]]:
        res = run_shim(
            venv_python=Path(sys.executable),
            shim_name="echo",
            action="validate",
            strategy_id="x",
            param_path="a",
            value=value,
            api_server_src=tmp_path,
        )
        assert res.ok is True
        assert res.new == value, f"value round-trip failed for {value!r}"

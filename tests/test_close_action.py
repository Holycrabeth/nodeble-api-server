"""Tests for actions.run_strategy_close — the M3.b subprocess wrapper.

Mirrors test_actions.py shape (M3.a scan): use stub strategy module on
disk that writes a known JSON contract to stdout + sets exit code, run
through real subprocess.run via the actions wrapper, assert the parsed
CloseResult.

The behaviors pinned here are the ones a regression would break loudly:
- ARCH-18 §2.3 exit-code → task_status mapping (0/1/2/3/4/5)
- ARCH-18 §2.4 final-stdout-line JSON parsing (handles trailing newlines,
  multi-line stdout where only the last line is JSON)
- argv shape: --mode close, --position-id <id>, --dry-run conditional
- subprocess timeout → task_status="timeout" with stderr captured
- unknown strategy / missing venv → task_status="spawn_error"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nodeble_api_server import actions
from nodeble_api_server.actions import (
    CloseResult,
    DEFAULT_CLOSE_TIMEOUT_SEC,
    _parse_module_close_payload,
    run_strategy_close,
)


# ── Stubbing helpers (mirror test_actions.py pattern) ───────────────────────


def _install_stub_close_module(
    tmp_path: Path,
    monkeypatch,
    *,
    stub_body: str,
    strategy_id: str = "wheel",
    venv_exists: bool = True,
) -> None:
    """Set up a tmp stub_pkg that is run via `python -m stub_pkg --mode
    close --position-id <id>`. The stub_body parses argv and emits a JSON
    contract on stdout, then exits with whatever sys.exit code it chooses.
    """
    stub_pkg = tmp_path / "stub_pkg"
    stub_pkg.mkdir()
    (stub_pkg / "__init__.py").write_text("")
    (stub_pkg / "__main__.py").write_text(stub_body)

    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(
        actions,
        "_strategy_package",
        lambda sid: "stub_pkg" if sid == strategy_id else None,
    )

    venv_path = Path(sys.executable) if venv_exists else Path("/nonexistent/python")
    monkeypatch.setattr(
        actions,
        "strategy_venv_python",
        lambda sid, home=None: venv_path,
    )


# ── _parse_module_close_payload (helper) ────────────────────────────────────


def test_parse_payload_finds_final_json_line():
    """Stdout ends with JSON line — parsed correctly."""
    stdout = (
        "2026-04-26 INFO loading state\n"
        "2026-04-26 INFO closing position\n"
        '{"status": "completed", "position_id": "X"}\n'
    )
    assert _parse_module_close_payload(stdout) == {
        "status": "completed",
        "position_id": "X",
    }


def test_parse_payload_skips_trailing_blank_lines():
    """Trailing empty lines after JSON don't break parsing."""
    stdout = '{"status": "failed"}\n\n\n'
    assert _parse_module_close_payload(stdout) == {"status": "failed"}


def test_parse_payload_returns_none_on_empty_stdout():
    assert _parse_module_close_payload("") is None
    assert _parse_module_close_payload("\n\n") is None


def test_parse_payload_returns_none_on_malformed_final_line():
    """If final line isn't valid JSON, return None (don't crash)."""
    stdout = "INFO something\nthis is not json\n"
    assert _parse_module_close_payload(stdout) is None


# ── run_strategy_close: exit code → task_status mapping ─────────────────────


_STUB_EMIT_AND_EXIT = '''\
import json, sys

# Parse args (--mode close --position-id <id> [--dry-run])
args = sys.argv[1:]
position_id = args[args.index("--position-id") + 1] if "--position-id" in args else "?"

# Read injected EXIT_CODE / PAYLOAD from env vars
import os
exit_code = int(os.environ.get("STUB_EXIT_CODE", "0"))
payload_str = os.environ.get("STUB_PAYLOAD", json.dumps({
    "status": "completed",
    "position_id": position_id,
    "closed_at": "2026-04-26T10:00:00-04:00",
    "fill_price": 1.50,
    "realized_pnl": 50.0,
    "per_leg_fills": [],
    "error": None,
}))

# Pretend we did some work (logs to stderr; final stdout line is JSON)
print("INFO loading state", file=sys.stderr)
print("INFO closing", file=sys.stderr)
print(payload_str)
sys.exit(exit_code)
'''


def test_close_completed_returns_task_status_completed(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_EMIT_AND_EXIT)
    monkeypatch.setenv("STUB_EXIT_CODE", "0")

    result = run_strategy_close("wheel", "test_pos_123")

    assert isinstance(result, CloseResult)
    assert result.task_status == "completed"
    assert result.exit_code == 0
    assert result.module_payload is not None
    assert result.module_payload["status"] == "completed"
    assert result.module_payload["position_id"] == "test_pos_123"
    assert result.error is None


def test_close_failed_returns_task_status_failed(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_EMIT_AND_EXIT)
    monkeypatch.setenv("STUB_EXIT_CODE", "1")
    monkeypatch.setenv("STUB_PAYLOAD", json.dumps({
        "status": "failed",
        "position_id": "x",
        "error": "broker error",
    }))

    result = run_strategy_close("wheel", "x")

    assert result.task_status == "failed"
    assert result.exit_code == 1
    assert result.error == "broker error"  # plucked from module_payload


def test_close_halted_returns_task_status_halted(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_EMIT_AND_EXIT)
    monkeypatch.setenv("STUB_EXIT_CODE", "2")
    monkeypatch.setenv("STUB_PAYLOAD", json.dumps({
        "status": "failed",
        "position_id": "x",
        "error": "strategy halted: drift detected",
    }))

    result = run_strategy_close("wheel", "x")

    assert result.task_status == "halted"
    assert result.exit_code == 2


def test_close_not_found_returns_task_status_not_found(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_EMIT_AND_EXIT)
    monkeypatch.setenv("STUB_EXIT_CODE", "3")
    monkeypatch.setenv("STUB_PAYLOAD", json.dumps({
        "status": "failed",
        "position_id": "x",
        "error": "position not found",
    }))

    result = run_strategy_close("wheel", "x")

    assert result.task_status == "not_found"
    assert result.exit_code == 3


def test_close_already_closed_returns_task_status_already_closed(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_EMIT_AND_EXIT)
    monkeypatch.setenv("STUB_EXIT_CODE", "4")
    monkeypatch.setenv("STUB_PAYLOAD", json.dumps({
        "status": "failed",
        "position_id": "x",
        "error": "position status=closed_profit",
    }))

    result = run_strategy_close("wheel", "x")

    assert result.task_status == "already_closed"
    assert result.exit_code == 4


def test_close_partial_fill_returns_task_status_partial_fill(tmp_path, monkeypatch):
    """Exit 5 — CRITICAL — partial fill must be distinguishable from
    generic failure so the frontend can show the per-leg-fills detail."""
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_EMIT_AND_EXIT)
    monkeypatch.setenv("STUB_EXIT_CODE", "5")
    monkeypatch.setenv("STUB_PAYLOAD", json.dumps({
        "status": "failed",
        "position_id": "x",
        "error": "partial fill — see per_leg_fills",
        "per_leg_fills": [
            {"identifier": "SPY  260430C690", "side": "short", "status": "filled"},
            {"identifier": "SPY  260430C700", "side": "long", "status": "unfilled"},
        ],
    }))

    result = run_strategy_close("wheel", "x")

    assert result.task_status == "partial_fill"
    assert result.exit_code == 5
    assert result.module_payload["per_leg_fills"][1]["status"] == "unfilled"


# ── argv shape ──────────────────────────────────────────────────────────────


_STUB_ECHO_ARGV = '''\
import json, sys
print(json.dumps({
    "argv": sys.argv,
    "status": "completed",
    "position_id": "echo",
    "closed_at": None, "fill_price": None, "realized_pnl": None,
    "per_leg_fills": [], "error": None,
}))
sys.exit(0)
'''


def test_close_argv_omits_dry_run_when_dry_run_false(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_ECHO_ARGV)
    result = run_strategy_close("wheel", "abc", dry_run=False)

    argv = result.module_payload["argv"]
    # argv[0] is the script path; the stub_pkg flags follow
    assert "--mode" in argv and "close" in argv
    assert "--position-id" in argv
    assert "abc" in argv
    assert "--dry-run" not in argv


def test_close_argv_includes_dry_run_when_dry_run_true(tmp_path, monkeypatch):
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_ECHO_ARGV)
    result = run_strategy_close("wheel", "abc", dry_run=True)

    argv = result.module_payload["argv"]
    assert "--dry-run" in argv


# ── Pre-subprocess failures ─────────────────────────────────────────────────


def test_close_unknown_strategy_returns_spawn_error(tmp_path, monkeypatch):
    """Strategy not in registry → spawn_error before subprocess."""
    monkeypatch.setattr(actions, "_strategy_package", lambda sid: None)

    result = run_strategy_close("nonexistent", "x")

    assert result.task_status == "spawn_error"
    assert result.exit_code is None
    assert result.module_payload is None
    assert "unknown strategy" in result.error


def test_close_missing_venv_returns_spawn_error(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "_strategy_package", lambda sid: "stub_pkg")
    monkeypatch.setattr(
        actions, "strategy_venv_python",
        lambda sid, home=None: Path("/nonexistent/path/python"),
    )

    result = run_strategy_close("wheel", "x")

    assert result.task_status == "spawn_error"
    assert "venv" in result.error.lower()


# ── Timeout ─────────────────────────────────────────────────────────────────


_STUB_HANG = '''\
import time
time.sleep(10)
'''


def test_close_timeout_returns_task_status_timeout(tmp_path, monkeypatch):
    """Subprocess that never emits → SIGKILL after timeout, task_status=timeout."""
    _install_stub_close_module(tmp_path, monkeypatch, stub_body=_STUB_HANG)

    result = run_strategy_close("wheel", "x", timeout_sec=0.5)

    assert result.task_status == "timeout"
    assert result.exit_code is None
    assert "timed out" in result.error

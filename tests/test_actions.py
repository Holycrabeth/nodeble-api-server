"""Tests for actions.run_strategy_scan — the subprocess-per-CLI layer.

Same strategy as test_config_writer: don't depend on any real strategy
venv or module. Point at the host's python interpreter via monkeypatch,
use tiny on-disk stub scripts to drive exit codes / output / sleep.

The behaviors we pin down are the ones most likely to break first:
- argv shape (--dry-run toggles on mode, --force always for MVP)
- timeout → SIGKILL (hung scan is the worst operator experience)
- nonzero exit → we still surface stdout/stderr, not swallow it
- unknown strategy / missing venv → clean error, not traceback
- stdout tail truncation (scan logs can be thousands of lines)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nodeble_api_server import actions
from nodeble_api_server.actions import (
    DEFAULT_SCAN_TIMEOUT_SEC,
    ScanResult,
    TAIL_LINES,
    _tail,
    run_strategy_scan,
)


# ── Stubbing helpers ────────────────────────────────────────────────────────


def _install_stub_strategy(
    tmp_path: Path,
    monkeypatch,
    *,
    stub_body: str,
    strategy_id: str = "wheel",
    venv_exists: bool = True,
) -> list[list[str]]:
    """Prepare a fake `nodeble_wheel` module on disk and monkeypatch
    `actions._strategy_package` + `strategy_venv_python` so the
    real function runs our stub instead of hitting the strategy repo.

    Returns a list that captures the argv of each subprocess invocation,
    letting tests assert cmd shape.
    """
    stub_pkg = tmp_path / "stub_pkg"
    stub_pkg.mkdir()
    (stub_pkg / "__init__.py").write_text("")
    (stub_pkg / "__main__.py").write_text(stub_body)

    # We run `python -m stub_pkg` but force the working directory to
    # include stub_pkg's parent so imports resolve. Easier: just run
    # the stub file directly and pretend the argv has --mode scan etc.
    # but that breaks the "--mode scan --dry-run --force" assertion
    # because the stub can't tell argv from `python -m`.
    # Solution: set PYTHONPATH=tmp_path so `python -m stub_pkg` works.
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    # Map strategy_id → "stub_pkg" so our CLI dispatch hits the stub.
    monkeypatch.setattr(
        actions,
        "_strategy_package",
        lambda sid: "stub_pkg" if sid == strategy_id else None,
    )

    # Venv resolution → use the host python (it's always present in CI).
    venv_path = Path(sys.executable) if venv_exists else Path("/nonexistent/python")
    monkeypatch.setattr(
        actions,
        "strategy_venv_python",
        lambda sid, home=None: venv_path,
    )

    return []  # argv capture left for subprocess.run mocking when needed


# ── run_strategy_scan: happy path ───────────────────────────────────────────


def test_scan_success_exit_zero(tmp_path: Path, monkeypatch):
    """Stub prints 3 lines and exits 0 → status='success', stdout surfaced."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys\n"
            "print('line 1')\n"
            "print('line 2')\n"
            "print('line 3')\n"
            "sys.exit(0)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=True, timeout_sec=5)
    assert r.status == "success"
    assert r.exit_code == 0
    assert "line 1" in r.stdout_tail
    assert "line 3" in r.stdout_tail
    assert r.error is None
    assert r.duration_ms >= 0
    # Timestamps are set by the actions layer; they should be ISO 8601.
    assert "T" in r.started_at and "T" in r.completed_at


def test_scan_nonzero_exit_surfaces_stderr(tmp_path: Path, monkeypatch):
    """Reconcile HALT (exit 3) → status='exit_nonzero', stderr surfaced."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys\n"
            "print('normal log', file=sys.stdout)\n"
            "print('reconcile halt: drift detected', file=sys.stderr)\n"
            "sys.exit(3)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=True, timeout_sec=5)
    assert r.status == "exit_nonzero"
    assert r.exit_code == 3
    assert "normal log" in r.stdout_tail
    assert "reconcile halt" in r.stderr_tail
    assert r.error is not None and "3" in r.error


# ── CLI argv shape ──────────────────────────────────────────────────────────


def test_dry_run_adds_flag(tmp_path: Path, monkeypatch):
    """mode='dry_run' puts --dry-run in argv. We prove it by having the
    stub itself check sys.argv and exit differently."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys\n"
            "assert '--dry-run' in sys.argv, f'missing --dry-run in {sys.argv}'\n"
            "assert '--force' in sys.argv, f'missing --force in {sys.argv}'\n"
            "assert '--mode' in sys.argv and 'scan' in sys.argv\n"
            "sys.exit(0)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=True, timeout_sec=5)
    assert r.status == "success", f"stub assertion failed: {r.stderr_tail}"


def test_live_mode_omits_dry_run_flag(tmp_path: Path, monkeypatch):
    """mode='live' must NOT append --dry-run. Stub checks and exits."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys\n"
            "assert '--dry-run' not in sys.argv, f'--dry-run leaked into live: {sys.argv}'\n"
            "assert 'scan' in sys.argv\n"
            "sys.exit(0)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="live", force=True, timeout_sec=5)
    assert r.status == "success", f"stub assertion failed: {r.stderr_tail}"


def test_force_false_omits_force_flag(tmp_path: Path, monkeypatch):
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys\n"
            "assert '--force' not in sys.argv\n"
            "sys.exit(0)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=False, timeout_sec=5)
    assert r.status == "success"


# ── Error paths ─────────────────────────────────────────────────────────────


def test_timeout_reports_timeout_status(tmp_path: Path, monkeypatch):
    """Stub sleeps past timeout → SIGKILL + status='timeout'."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys, time\n"
            "print('about to hang', flush=True)\n"
            "time.sleep(5)\n"
            "sys.exit(0)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=True, timeout_sec=0.5)
    assert r.status == "timeout"
    assert r.exit_code is None
    assert r.error is not None and "timed out" in r.error
    assert r.duration_ms >= 0


def test_unknown_strategy_returns_spawn_error(tmp_path: Path, monkeypatch):
    """Unknown strategy id → _strategy_package() returns None → spawn_error
    before we try to run anything."""
    monkeypatch.setattr(actions, "_strategy_package", lambda sid: None)
    r = run_strategy_scan("nosuch", mode="dry_run", force=True, timeout_sec=5)
    assert r.status == "spawn_error"
    assert r.exit_code is None
    assert r.error is not None and "nosuch" in r.error


def test_missing_venv_returns_spawn_error(tmp_path: Path, monkeypatch):
    """venv path resolves but the file doesn't exist → spawn_error, not
    FileNotFoundError leaking to the caller."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body="import sys; sys.exit(0)",
        venv_exists=False,
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=True, timeout_sec=5)
    assert r.status == "spawn_error"
    assert r.error is not None and "venv" in r.error


def test_invalid_mode_raises_valueerror(monkeypatch):
    """mode other than 'dry_run' / 'live' is a programming error, not a
    runtime condition — raise immediately so callers notice."""
    with pytest.raises(ValueError, match="dry_run"):
        run_strategy_scan("wheel", mode="paper", timeout_sec=1)


# ── Output truncation ───────────────────────────────────────────────────────


def test_tail_truncates_to_limit():
    """Raw helper test — scan stdout can be thousands of lines, we only
    keep the last N."""
    body = "\n".join(f"line {i}" for i in range(200))
    out = _tail(body, lines=50)
    lines = out.splitlines()
    assert len(lines) == 50
    assert lines[0] == "line 150"
    assert lines[-1] == "line 199"


def test_tail_empty_returns_empty():
    assert _tail("") == ""
    assert _tail(None or "") == ""


def test_large_stdout_is_tailed_by_default(tmp_path: Path, monkeypatch):
    """Integration: stub prints 100 lines, we only surface last TAIL_LINES."""
    _install_stub_strategy(
        tmp_path,
        monkeypatch,
        stub_body=(
            "import sys\n"
            "for i in range(100):\n"
            "    print(f'output line {i}')\n"
            "sys.exit(0)\n"
        ),
    )
    r = run_strategy_scan("wheel", mode="dry_run", force=True, timeout_sec=5)
    assert r.status == "success"
    lines = r.stdout_tail.splitlines()
    assert len(lines) == TAIL_LINES
    # The last line printed should be in the tail
    assert "output line 99" in r.stdout_tail
    # The first line printed should NOT be in the tail
    assert "output line 0" not in r.stdout_tail


# ── Package name resolution (IC is the weird one) ───────────────────────────


def test_strategy_package_ic_is_nodeble():
    """IC's module is 'nodeble' (no suffix) — historical, not `nodeble_ic`."""
    assert actions._strategy_package("ic") == "nodeble"


def test_strategy_package_others_use_underscore():
    assert actions._strategy_package("wheel") == "nodeble_wheel"
    assert actions._strategy_package("directionalspread") == "nodeble_directionalspread"


def test_strategy_package_unknown_returns_none():
    assert actions._strategy_package("zzz_not_real") is None

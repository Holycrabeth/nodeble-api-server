"""Tests for install_runner.py (Phase A Week 3 — 5/5 contract).

Two layers:

- Pure parser tests (`_parse_line`) — fast, no subprocess.
- Integration tests (`run_install`) — real subprocess via tiny `bash -c`
  scripts that emit the 5/5 STEP/STATUS/RESULT contract on stdout. This
  exercises the full pipeline (subprocess spawn, line drain, state
  persistence, terminal handling) without needing a real deploy.sh on
  disk — Wheel/IC/PMCC/DS deploy.sh ships are blocked on Phase C+D and
  not needed for parser-side acceptance.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nodeble_api_server import install_runner, install_state


# ── _parse_line: 5/5 contract shapes ────────────────────────────────────────


def test_parse_step_start():
    p = install_runner._parse_line("STEP: clone-repo")
    assert p.event_type == "step"
    assert p.payload == {"step": "clone-repo", "status": "in_progress"}
    assert p.is_terminal is False


def test_parse_step_success_with_check_glyph():
    p = install_runner._parse_line("STEP: clone-repo ✓")
    assert p.event_type == "step"
    assert p.payload == {"step": "clone-repo", "status": "ok"}


def test_parse_step_failure_with_summary():
    p = install_runner._parse_line("STEP: clone-repo ✗ git clone failed")
    assert p.event_type == "step"
    assert p.payload["status"] == "failed"
    assert p.payload["error"] == "git clone failed"
    assert p.payload["step"] == "clone-repo"


def test_parse_step_failure_no_summary_substitutes_default():
    p = install_runner._parse_line("STEP: clone-repo ✗")
    assert p.event_type == "step"
    assert p.payload["status"] == "failed"
    assert p.payload["error"] == "step failed (no summary)"


def test_parse_status_success_terminal():
    p = install_runner._parse_line("STATUS: success")
    assert p.event_type == "status"
    assert p.payload == {"status": "success"}
    assert p.is_terminal is True


def test_parse_status_already_installed_terminal():
    p = install_runner._parse_line("STATUS: already_installed")
    assert p.event_type == "status"
    assert p.payload == {"status": "already_installed"}
    assert p.is_terminal is True


def test_parse_status_failure_terminal_carries_reason():
    p = install_runner._parse_line("STATUS: failure: clone_failed")
    assert p.event_type == "status"
    assert p.payload == {"status": "failed", "error": "clone_failed"}
    assert p.is_terminal is True


def test_parse_result_metadata():
    p = install_runner._parse_line("RESULT_VERSION: 0.7.2")
    assert p.event_type == "result"
    assert p.payload == {"key": "VERSION", "value": "0.7.2"}


def test_parse_result_iso_timestamp_value():
    p = install_runner._parse_line("RESULT_INSTALLED_AT: 2026-05-05T13:30:00Z")
    assert p.event_type == "result"
    assert p.payload == {"key": "INSTALLED_AT", "value": "2026-05-05T13:30:00Z"}


def test_parse_bare_stdout_line_is_log_info():
    p = install_runner._parse_line("downloading deps...", is_stderr=False)
    assert p.event_type == "log"
    assert p.payload == {"level": "info", "message": "downloading deps..."}


def test_parse_bare_stderr_line_is_log_warn():
    p = install_runner._parse_line("warning: 1 deprecation", is_stderr=True)
    assert p.event_type == "log"
    assert p.payload == {"level": "warn", "message": "warning: 1 deprecation"}


def test_parse_status_precedence_over_step_prefix_collision():
    """Defensive: 'STATUS: success' must not be mistaken for a STEP line."""
    p = install_runner._parse_line("STATUS: success")
    assert p.event_type == "status"
    assert p.event_type != "step"


def test_parse_handles_trailing_whitespace_and_crlf():
    p = install_runner._parse_line("STATUS: success \r\n")
    assert p.event_type == "status"
    assert p.payload["status"] == "success"


def test_parse_unknown_status_falls_through_to_log():
    """STATUS: foo (not success/already_installed/failure:) → log line."""
    p = install_runner._parse_line("STATUS: pending")
    assert p.event_type == "log"


# ── run_install: integration via fake bash scripts ──────────────────────────


@pytest.fixture
def fake_install(tmp_path: Path):
    """Create an install_state entry for tests + return install_id + tmp home."""
    install_id = "test-install-1"
    install_state.create(
        install_id=install_id,
        strategy="wheel",
        config={"capital_usd": 50000},
        home=tmp_path,
    )
    return install_id, tmp_path


def _bash_cmd(*lines: str) -> list[str]:
    """Build a tiny bash -c that emits the given lines on stdout, then exits."""
    body = "\n".join(f"echo '{line}'" for line in lines)
    return ["bash", "-c", body]


def test_run_install_happy_path_success(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd(
        "STEP: clone-repo",
        "STEP: clone-repo ✓",
        "STEP: venv-create",
        "STEP: venv-create ✓",
        "RESULT_VERSION: 0.7.2",
        "RESULT_SERVICE_NAME: nodeble-wheel-bot.service",
        "STATUS: success",
    )

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
    ))

    assert result["status"] == "success"
    assert result["result_metadata"] == {
        "VERSION": "0.7.2",
        "SERVICE_NAME": "nodeble-wheel-bot.service",
    }

    # State.json reflects success
    state = install_state.read(install_id, home=home)
    assert state["status"] == "success"
    assert state["completed_at"] is not None
    assert state["error"] is None
    assert state["result_metadata"] == {
        "VERSION": "0.7.2",
        "SERVICE_NAME": "nodeble-wheel-bot.service",
    }


def test_run_install_already_installed_terminal(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd("STATUS: already_installed")

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
    ))

    assert result["status"] == "already_installed"
    state = install_state.read(install_id, home=home)
    assert state["status"] == "already_installed"


def test_run_install_failure_with_reason(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd(
        "STEP: clone-repo",
        "STEP: clone-repo ✗ permission denied",
        "STATUS: failure: clone_failed",
    )

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
    ))

    assert result["status"] == "failed"
    assert "clone_failed" in result["error"]
    state = install_state.read(install_id, home=home)
    assert state["status"] == "failed"


def test_run_install_subprocess_exit_zero_no_status_treated_failed(fake_install):
    """Contract violation: deploy.sh exits 0 without STATUS terminal → failed."""
    install_id, home = fake_install
    cmd = ["bash", "-c", "echo 'some progress'; exit 0"]

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
    ))

    assert result["status"] == "failed"
    assert "STATUS terminal" in result["error"]


def test_run_install_subprocess_exits_nonzero_with_no_status(fake_install):
    install_id, home = fake_install
    cmd = ["bash", "-c", "echo 'oops'; exit 7"]

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
    ))

    assert result["status"] == "failed"
    assert "exited with code 7" in result["error"]


def test_run_install_emits_complete_event_to_jsonl(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd("STATUS: success")
    asyncio.run(install_runner.run_install(install_id=install_id, cmd=cmd, home=home))

    events = list(install_state.replay_events(install_id, home=home))
    # Last event should be the 'complete' terminal
    assert events[-1]["event"] == "complete"
    assert events[-1]["data"]["status"] == "success"


def test_run_install_steps_completed_recorded(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd(
        "STEP: clone-repo",
        "STEP: clone-repo ✓",
        "STEP: venv-create",
        "STEP: venv-create ✓",
        "STATUS: success",
    )
    asyncio.run(install_runner.run_install(install_id=install_id, cmd=cmd, home=home))

    state = install_state.read(install_id, home=home)
    completed = state["steps_completed"]
    assert len(completed) == 2
    assert completed[0]["step"] == "clone-repo"
    assert completed[0]["status"] == "ok"
    assert completed[1]["step"] == "venv-create"


def test_run_install_failed_step_recorded_in_steps_completed(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd(
        "STEP: clone-repo",
        "STEP: clone-repo ✗ network",
        "STATUS: failure: clone_failed",
    )
    asyncio.run(install_runner.run_install(install_id=install_id, cmd=cmd, home=home))

    state = install_state.read(install_id, home=home)
    completed = state["steps_completed"]
    assert len(completed) == 1
    assert completed[0]["step"] == "clone-repo"
    assert completed[0]["status"] == "failed"
    assert completed[0]["error"] == "network"


def test_run_install_log_lines_appended_to_log_tail(fake_install):
    install_id, home = fake_install
    cmd = _bash_cmd(
        "downloading 12345 bytes",
        "STATUS: success",
    )
    asyncio.run(install_runner.run_install(install_id=install_id, cmd=cmd, home=home))

    state = install_state.read(install_id, home=home)
    log_messages = [e["message"] for e in state["log_tail"]]
    assert "downloading 12345 bytes" in log_messages


def test_run_install_subprocess_failed_to_start(fake_install):
    """Bash binary missing or path invalid → graceful failed event."""
    install_id, home = fake_install
    cmd = ["/nonexistent/bin/totally-not-a-thing"]

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
    ))

    assert result["status"] == "failed"
    assert "subprocess failed to start" in result["error"]
    state = install_state.read(install_id, home=home)
    assert state["status"] == "failed"


def test_run_install_total_budget_timeout_sigterm(fake_install):
    """Budget < script duration → SIGTERM + failed."""
    install_id, home = fake_install
    cmd = ["bash", "-c", "sleep 10"]

    result = asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
        total_budget_ms=200,
    ))

    assert result["status"] == "failed"
    assert "exceeded budget" in result["error"]


# ── build_deploy_cmd ────────────────────────────────────────────────────────


def test_build_deploy_cmd_basic(tmp_path: Path):
    cfg = tmp_path / "wheel-config.json"
    cfg.write_text("{}")
    deploy_root = tmp_path / "nodeble-wheel"
    cmd = install_runner.build_deploy_cmd(
        strategy="wheel", config_json_path=cfg, deploy_root=deploy_root,
    )
    assert cmd[0] == "bash"
    assert cmd[1] == str(deploy_root / "deploy" / "deploy.sh")
    assert "--non-interactive" in cmd
    assert "--config" in cmd
    assert str(cfg) in cmd


def test_build_deploy_cmd_with_extra_args(tmp_path: Path):
    cfg = tmp_path / "config.json"
    deploy_root = tmp_path / "nodeble"
    cmd = install_runner.build_deploy_cmd(
        strategy="ic", config_json_path=cfg, deploy_root=deploy_root,
        extra_args=["--skip-telegram"],
    )
    assert cmd[-1] == "--skip-telegram"

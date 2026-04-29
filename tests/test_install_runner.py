"""Tests for install_runner — Phase A Week 3 DRAFT.

Parser tests are pure unit (no I/O). Subprocess tests use a tiny bash
script that emits STEP/STATUS lines + sleep, validating that
run_install correctly:
  - parses STEP / STATUS ok / STATUS fail
  - emits 'log' events for bare lines + stderr
  - emits 'complete' on subprocess exit
  - times out and SIGTERMs on budget excess
  - handles non-zero exit code with no STATUS: fail
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from nodeble_api_server import install_runner, install_state


# ── Parser unit tests (pure, no I/O) ────────────────────────────────────────


def test_parse_step_line():
    p = install_runner._parse_line("STEP: Cloning nodeble-wheel\n")
    assert p.event_type == "step"
    assert p.payload == {"step": "Cloning nodeble-wheel", "status": "in_progress"}
    assert p.is_terminal is False


def test_parse_status_ok_no_message():
    p = install_runner._parse_line("STATUS: ok\n")
    assert p.event_type == "step"
    assert p.payload == {"status": "ok"}
    assert "message" not in p.payload


def test_parse_status_ok_with_message():
    p = install_runner._parse_line("STATUS: ok venv created\n")
    assert p.event_type == "step"
    assert p.payload == {"status": "ok", "message": "venv created"}


def test_parse_status_fail_is_terminal():
    p = install_runner._parse_line("STATUS: fail pip install failed: SSL cert error\n")
    assert p.event_type == "step"
    assert p.payload["status"] == "failed"
    assert "SSL cert error" in p.payload["error"]
    assert p.is_terminal is True


def test_parse_bare_line_is_log_info():
    p = install_runner._parse_line("Cloning into 'nodeble-wheel'...\n")
    assert p.event_type == "log"
    assert p.payload == {"level": "info", "message": "Cloning into 'nodeble-wheel'..."}


def test_parse_stderr_line_is_log_warn():
    p = install_runner._parse_line("warning: deprecated\n", is_stderr=True)
    assert p.event_type == "log"
    assert p.payload["level"] == "warn"


def test_parse_long_line_does_not_crash():
    """200+ char STATUS line — parser must not crash even if deploy.sh
    forgot to truncate (Wheel dev Q4 contract says 200-char cap, but
    we should be defensive)."""
    long_msg = "X" * 500
    p = install_runner._parse_line(f"STATUS: fail {long_msg}\n")
    assert p.event_type == "step"
    assert p.payload["status"] == "failed"
    assert long_msg in p.payload["error"]


def test_parse_whitespace_tolerant():
    p = install_runner._parse_line("STEP:    Setting up venv   \n")
    assert p.payload["step"] == "Setting up venv"


def test_parse_empty_line_is_empty_log():
    p = install_runner._parse_line("\n")
    assert p.event_type == "log"
    assert p.payload["message"] == ""


def test_parse_step_without_colon_is_log():
    """`STEP foo` (no colon) is NOT a step — it's a log line."""
    p = install_runner._parse_line("STEP foo\n")
    assert p.event_type == "log"


# ── Subprocess integration tests (mock bash script) ─────────────────────────


@pytest.fixture
def mock_deploy_script(tmp_path: Path):
    """A bash script that emits STEP/STATUS lines for testing run_install."""
    def _build(content: str) -> Path:
        path = tmp_path / "mock_deploy.sh"
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path
    return _build


def test_run_install_happy_path(tmp_path, mock_deploy_script):
    """Deploy script emits 2 steps, exits 0. Expect: 2 step events (in_prog+ok
    each) + 1 complete event with status=success."""
    script = mock_deploy_script(
        "#!/bin/bash\n"
        "echo 'STEP: Validating config'\n"
        "echo 'STATUS: ok'\n"
        "echo 'STEP: Cloning repo'\n"
        "echo 'Cloning into ...'\n"
        "echo 'STATUS: ok'\n"
    )

    install_state.create(
        install_id="t1", strategy="wheel", config={}, home=tmp_path,
    )

    async def go():
        return await install_runner.run_install(
            install_id="t1",
            cmd=["bash", str(script)],
            home=tmp_path,
        )

    result = asyncio.run(go())
    assert result["status"] == "success"
    assert result["duration_ms"] >= 0

    events = list(install_state.replay_events("t1", home=tmp_path))
    step_events = [e for e in events if e["event"] == "step"]
    log_events = [e for e in events if e["event"] == "log"]
    complete_events = [e for e in events if e["event"] == "complete"]
    assert len(step_events) == 4  # 2 steps × (start + ok)
    assert len(log_events) == 1   # "Cloning into ..."
    assert len(complete_events) == 1
    assert complete_events[0]["data"]["status"] == "success"


def test_run_install_status_fail_triggers_failed_complete(tmp_path, mock_deploy_script):
    """STATUS: fail → emits failed step + complete(failed)."""
    script = mock_deploy_script(
        "#!/bin/bash\n"
        "echo 'STEP: Setting up venv'\n"
        "echo 'STATUS: fail pip install failed'\n"
        "exit 1\n"
    )

    install_state.create(install_id="t2", strategy="wheel", config={}, home=tmp_path)

    async def go():
        return await install_runner.run_install(
            install_id="t2",
            cmd=["bash", str(script)],
            home=tmp_path,
        )

    result = asyncio.run(go())
    assert result["status"] == "failed"
    assert "pip install failed" in result["error"]

    state = install_state.read("t2", home=tmp_path)
    assert state["status"] == "failed"
    assert "pip install failed" in state["error"]


def test_run_install_nonzero_exit_no_status_fail(tmp_path, mock_deploy_script):
    """Subprocess exits non-zero without STATUS: fail → synthetic failed complete."""
    script = mock_deploy_script(
        "#!/bin/bash\n"
        "echo 'STEP: Doing thing'\n"
        "exit 42\n"  # crash without STATUS: fail
    )

    install_state.create(install_id="t3", strategy="wheel", config={}, home=tmp_path)

    async def go():
        return await install_runner.run_install(
            install_id="t3",
            cmd=["bash", str(script)],
            home=tmp_path,
        )

    result = asyncio.run(go())
    assert result["status"] == "failed"
    assert "42" in result["error"]


def test_run_install_budget_timeout(tmp_path, mock_deploy_script):
    """Subprocess sleeps past budget → SIGTERM + complete(failed) with budget error."""
    script = mock_deploy_script(
        "#!/bin/bash\n"
        "echo 'STEP: Long task'\n"
        "sleep 30\n"
    )

    install_state.create(install_id="t4", strategy="wheel", config={}, home=tmp_path)

    async def go():
        return await install_runner.run_install(
            install_id="t4",
            cmd=["bash", str(script)],
            total_budget_ms=200,  # 0.2s budget — script sleeps 30s
            home=tmp_path,
        )

    result = asyncio.run(go())
    assert result["status"] == "failed"
    assert "budget" in result["error"].lower()
    assert result["duration_ms"] < 10_000  # well under the 30s sleep


def test_run_install_state_progresses_to_running_then_terminal(tmp_path, mock_deploy_script):
    """Verify state.json reflects each lifecycle stage."""
    script = mock_deploy_script(
        "#!/bin/bash\n"
        "echo 'STEP: A'\n"
        "echo 'STATUS: ok'\n"
    )

    install_state.create(install_id="t5", strategy="wheel", config={}, home=tmp_path)
    pre_state = install_state.read("t5", home=tmp_path)
    assert pre_state["status"] == "queued"

    async def go():
        await install_runner.run_install(
            install_id="t5", cmd=["bash", str(script)], home=tmp_path,
        )

    asyncio.run(go())

    final_state = install_state.read("t5", home=tmp_path)
    assert final_state["status"] == "success"
    assert final_state["completed_at"] is not None
    assert len(final_state["steps_completed"]) == 1
    assert final_state["steps_completed"][0]["step"] == "A"


def test_build_deploy_cmd_format(tmp_path):
    """Smoke test for the cmd builder helper."""
    cfg_path = tmp_path / "cfg.json"
    deploy_root = tmp_path / "nodeble-wheel"
    cmd = install_runner.build_deploy_cmd(
        strategy="wheel",
        config_json_path=cfg_path,
        deploy_root=deploy_root,
    )
    assert cmd[0] == "bash"
    assert cmd[1] == str(deploy_root / "deploy" / "deploy.sh")
    assert "--non-interactive" in cmd
    assert "--config" in cmd
    assert str(cfg_path) in cmd

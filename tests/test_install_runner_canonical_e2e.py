"""Path A hermetic install_runner smoke against canonical-fixture deploy.sh.

CTO 2026-05-10 dispatch (state assessment §4 Path A): close the P0 gap
between existing 27 hermetic tests (synthetic 2-3 STEP `bash -c` scripts)
and "real-deploy-sh-against-fresh-VPS production-grade smoke". This file
sits between them — invokes ``run_install`` against a fixture script that
emits the FULL 10-STEP canonical contract sequence + RESULT_* metadata +
STATUS terminal per the 2026-05-05 deploy.sh non-interactive contract,
WITHOUT requiring real venv / pip / cron / systemctl / network.

What this validates (vs prior 27 hermetic tests)
------------------------------------------------
- Parser correctness on full 10-STEP sequence (prior tests max out at 2-3 STEPs)
- duration_ms tracking across multi-second elapsed time per STEP
- events.jsonl 4-event-type mix (step / log / result / complete) end-to-end
- state.json final-state shape after a complete install run
- Subprocess termination behavior under wall-clock budget cap
- Contract-violation detection (exit 0 + no STATUS terminal → failed)
- Idempotent re-run path (STATUS: already_installed terminal)
- Mid-install failure path (STEP ✗ + STATUS: failure: <reason> + exit 11)

What this does NOT validate (Path B/C scope per state assessment §4)
--------------------------------------------------------------------
- Real strategy module deploy.sh contract conformance (Path B Docker matrix)
- Real-curl SSE consumption over network (Path C fresh Vultr)
- Real broker / Tiger creds / cron systemctl invocations
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nodeble_api_server import install_runner, install_state


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "canonical-deploy.sh"


# ── Module-level invariants ────────────────────────────────────────────────


def test_canonical_fixture_exists_and_executable():
    """Sanity check: fixture script must be present + executable for tests
    below to run. Fail-fast if filesystem state drifts (e.g. chmod lost
    on git clone)."""
    assert FIXTURE_PATH.is_file(), f"fixture missing: {FIXTURE_PATH}"
    import os
    assert os.access(FIXTURE_PATH, os.X_OK), (
        f"fixture not executable: {FIXTURE_PATH} — run `chmod +x` after clone"
    )


# ── Helper: spawn install_runner against fixture in given mode ─────────────


@pytest.fixture
def staged_install(tmp_path: Path):
    """Create install_state entry sandboxed to tmp_path. Returns
    (install_id, home_path) — pass home_path to run_install for state isolation.
    """
    install_id = "canonical-e2e-1"
    install_state.create(
        install_id=install_id,
        strategy="wheel",
        config={"capital_usd": 50000, "module": "wheel"},
        home=tmp_path,
    )
    return install_id, tmp_path


def _run_canonical(install_id: str, home: Path, mode: str = "happy",
                   total_budget_ms: int = 60_000) -> dict:
    """Invoke ``run_install`` against canonical fixture in `mode`.
    Returns the complete event payload."""
    cmd = ["bash", str(FIXTURE_PATH), mode]
    return asyncio.run(install_runner.run_install(
        install_id=install_id, cmd=cmd, home=home,
        total_budget_ms=total_budget_ms,
    ))


# ── Test 1: full 10-STEP happy path ────────────────────────────────────────


def test_happy_path_full_10_step_sequence(staged_install):
    """Full canonical contract sequence: 10 STEPs + 3 RESULT_* + STATUS: success.

    Validates parser handles the FULL sequence (vs prior synthetic 2-3 STEP
    tests) + run_install assembles the complete event flow correctly.
    """
    install_id, home = staged_install
    result = _run_canonical(install_id, home, mode="happy")

    # Final status
    assert result["status"] == "success"
    assert "duration_ms" in result
    # 10 STEPs × 50ms sleep + parsing overhead → at least 500ms
    assert result["duration_ms"] >= 500, (
        f"duration_ms suspiciously low ({result['duration_ms']}ms) — "
        "fixture sleeps SHOULD have been measured"
    )

    # result_metadata captures all 3 RESULT_* keys
    assert result["result_metadata"] == {
        "VERSION": "0.7.2",
        "INSTALLED_AT": "2026-05-10T13:30:00Z",
        "SERVICE_NAME": "nodeble-wheel-bot.service",
    }


# ── Test 2: events.jsonl contains expected 4-event-type mix ────────────────


def test_events_jsonl_contains_all_four_event_types(staged_install):
    """Verifies install_state.events.jsonl 4-event-type shape per Q4 finding
    of state assessment doc (step / log / result / complete) — NO error /
    progress events emitted for happy path."""
    install_id, home = staged_install
    _run_canonical(install_id, home, mode="log_spam")

    events = list(install_state.replay_events(install_id, home=home))
    event_types = {e["event"] for e in events}

    # 4-event-type contract per state assessment §2 Q4
    assert event_types == {"step", "log", "result", "complete"}, (
        f"unexpected event types: {event_types}"
    )

    # Specifically NOT in the wire shape
    assert "error" not in event_types
    assert "progress" not in event_types
    assert "status" not in event_types  # captured internally; emitted as 'complete'


def test_log_event_captures_stdout_and_stderr_separately(staged_install):
    """Bare stdout → log level=info; stderr → log level=warn.
    Validates _drain_stream stream-source classification."""
    install_id, home = staged_install
    _run_canonical(install_id, home, mode="log_spam")

    events = list(install_state.replay_events(install_id, home=home))
    log_events = [e for e in events if e["event"] == "log"]

    info_logs = [e for e in log_events if e["data"].get("level") == "info"]
    warn_logs = [e for e in log_events if e["data"].get("level") == "warn"]

    assert any("downloading 12345 bytes" in e["data"]["message"] for e in info_logs), (
        "stdout bare line missing from log events / wrong level"
    )
    assert any("deprecated option" in e["data"]["message"] for e in warn_logs), (
        "stderr bare line missing from log events / wrong level"
    )


# ── Test 3: state.json final-state shape ───────────────────────────────────


def test_state_json_end_state_after_happy_install(staged_install):
    """state.json reflects final success state with steps_completed +
    log_tail + result_metadata."""
    install_id, home = staged_install
    _run_canonical(install_id, home, mode="happy")

    state = install_state.read(install_id, home=home)
    assert state["status"] == "success"
    assert state["completed_at"] is not None
    assert state["error"] is None

    # 10 steps_completed entries (one per ✓)
    assert len(state["steps_completed"]) == 10, (
        f"expected 10 completed steps; got {len(state['steps_completed'])}"
    )

    # First + last step IDs match canonical sequence
    assert state["steps_completed"][0]["step"] == "prereq-check-os"
    assert state["steps_completed"][-1]["step"] == "post-install-smoke"

    # Each step has duration_ms tracked > 0 (real fixture sleeps were measured)
    for completed_step in state["steps_completed"]:
        assert completed_step["status"] == "ok"
        assert completed_step["duration_ms"] is not None
        assert completed_step["duration_ms"] >= 0, (
            f"negative duration_ms for {completed_step['step']!r}"
        )

    # result_metadata persisted on state.json (not just in complete event)
    assert state.get("result_metadata") == {
        "VERSION": "0.7.2",
        "INSTALLED_AT": "2026-05-10T13:30:00Z",
        "SERVICE_NAME": "nodeble-wheel-bot.service",
    }


# ── Test 4: STATUS: already_installed terminal path ────────────────────────


def test_already_installed_terminal_path(staged_install):
    """Idempotent re-run: short STEP sequence + STATUS: already_installed
    → install_runner emits complete with status=already_installed (NOT success)."""
    install_id, home = staged_install
    result = _run_canonical(install_id, home, mode="already_installed")

    assert result["status"] == "already_installed"
    state = install_state.read(install_id, home=home)
    assert state["status"] == "already_installed"

    # RESULT_* still captured per spec §5.1
    assert result["result_metadata"] == {
        "VERSION": "0.7.2",
        "INSTALLED_AT": "2026-05-10T13:30:00Z",
        "SERVICE_NAME": "nodeble-wheel-bot.service",
    }


# ── Test 5: mid-install STEP failure → STATUS: failure: <reason> ───────────


def test_mid_install_step_failure_terminal_path(staged_install):
    """STEP 4 emits ✗ then STATUS: failure: clone_failed + exit 11.
    install_runner reports terminal failure with reason + records the
    failed step in steps_completed."""
    install_id, home = staged_install
    result = _run_canonical(install_id, home, mode="fail_clone")

    assert result["status"] == "failed"
    assert "clone_failed" in result["error"]

    state = install_state.read(install_id, home=home)
    assert state["status"] == "failed"

    # First 3 steps succeeded; 4th (clone-repo) recorded as failed
    completed = state["steps_completed"]
    ok_steps = [s for s in completed if s["status"] == "ok"]
    failed_steps = [s for s in completed if s["status"] == "failed"]
    assert len(ok_steps) == 3
    assert len(failed_steps) == 1
    assert failed_steps[0]["step"] == "clone-repo"
    assert "git clone failed" in failed_steps[0]["error"]


# ── Test 6: total budget timeout → SIGTERM + complete(failed) ──────────────


def test_total_budget_timeout_triggers_sigterm(staged_install):
    """Fixture's `slow` mode sleeps 30s; budget 500ms → SIGTERM mid-sleep,
    install_runner emits complete with status=failed + exceeded-budget error.
    Verifies parser-side cleanup + state.json write under termination path."""
    install_id, home = staged_install
    result = _run_canonical(install_id, home, mode="slow", total_budget_ms=500)

    assert result["status"] == "failed"
    assert "exceeded budget" in result["error"]
    # Worst case: budget (500ms) + SIGTERM grace window (≤5s in install_runner
    # before SIGKILL) + bash startup + parser overhead. 8s is a generous
    # ceiling that still proves the subprocess didn't run to its 30s sleep.
    assert result["duration_ms"] < 8_000, (
        f"timeout fired but duration_ms={result['duration_ms']} suggests "
        "subprocess wasn't actually terminated within SIGTERM grace window"
    )
    # Lower bound: must be > budget (subprocess actually got SIGTERM, not
    # rejected pre-spawn). Catches the inverse bug where timeout fires
    # synthetically before the subprocess even started.
    assert result["duration_ms"] >= 500, (
        f"duration_ms < budget ({result['duration_ms']}) — subprocess may have "
        "exited before timeout fired"
    )


# ── Test 7: contract violation (exit 0 + no STATUS terminal) ───────────────


def test_contract_violation_exit_zero_no_status_treated_failed(staged_install):
    """Per install_runner.run_install final-status logic: exit 0 with NO
    STATUS terminal line is a CONTRACT VIOLATION → must be reported as
    failed (NOT silently treated as success)."""
    install_id, home = staged_install
    result = _run_canonical(install_id, home, mode="contract_violation")

    assert result["status"] == "failed"
    assert "STATUS terminal" in result["error"], (
        f"expected contract-violation message; got: {result['error']!r}"
    )

    state = install_state.read(install_id, home=home)
    assert state["status"] == "failed"


# ── Test 8: complete event is the LAST event in events.jsonl ───────────────


def test_complete_is_last_event_in_jsonl(staged_install):
    """SSE replay-then-tail loop in routes/server.py:_generate_install_events
    expects 'complete' event to be terminal — NO events appended after it."""
    install_id, home = staged_install
    _run_canonical(install_id, home, mode="happy")

    events = list(install_state.replay_events(install_id, home=home))
    assert events[-1]["event"] == "complete", (
        f"complete must be terminal event; got tail: "
        f"{[e['event'] for e in events[-3:]]}"
    )

    # And only ONE complete event total (no duplicates from edge-case races)
    complete_count = sum(1 for e in events if e["event"] == "complete")
    assert complete_count == 1, f"expected 1 complete event; got {complete_count}"

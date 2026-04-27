"""Tests for install_state persistence — Phase A Week 2.

Pin: state.json + events.jsonl persist; idempotent create; SSE replay
yields events in order; cleanup_stale_running marks active installs as
failed on api-server restart simulation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodeble_api_server import install_state


def test_create_writes_state_and_returns_dict(tmp_path):
    state = install_state.create(
        install_id="test-1",
        strategy="wheel",
        config={"budget": 30000},
        home=tmp_path,
    )
    assert state["install_id"] == "test-1"
    assert state["strategy"] == "wheel"
    assert state["status"] == "queued"
    assert state["started_at"] is not None
    # Disk verify
    state_path = tmp_path / ".nodeble-api" / "data" / "installs" / "test-1" / "state.json"
    assert state_path.exists()
    on_disk = json.loads(state_path.read_text())
    assert on_disk["install_id"] == "test-1"


def test_create_idempotent_same_install_id(tmp_path):
    """Two creates with same install_id return same state, no overwrite."""
    state1 = install_state.create(
        install_id="dup-test", strategy="wheel", config={}, home=tmp_path,
    )
    state2 = install_state.create(
        install_id="dup-test", strategy="ic", config={}, home=tmp_path,
    )
    # Second call returns existing state — strategy preserved from first
    assert state2["strategy"] == "wheel"
    assert state2["started_at"] == state1["started_at"]


def test_read_returns_none_for_unknown_install_id(tmp_path):
    assert install_state.read("never-existed", home=tmp_path) is None


def test_read_after_create_returns_state(tmp_path):
    install_state.create(
        install_id="read-test", strategy="wheel", config={}, home=tmp_path,
    )
    state = install_state.read("read-test", home=tmp_path)
    assert state is not None
    assert state["install_id"] == "read-test"


def test_update_state_patches_fields(tmp_path):
    install_state.create(install_id="upd-test", strategy="wheel", config={}, home=tmp_path)
    updated = install_state.update_state(
        "upd-test",
        status="running",
        current_step="Cloning repo",
        home=tmp_path,
    )
    assert updated["status"] == "running"
    assert updated["current_step"] == "Cloning repo"
    # Verify on disk
    fresh = install_state.read("upd-test", home=tmp_path)
    assert fresh["status"] == "running"
    assert fresh["current_step"] == "Cloning repo"


def test_update_state_appends_to_steps_completed(tmp_path):
    install_state.create(install_id="x", strategy="wheel", config={}, home=tmp_path)
    install_state.update_state(
        "x",
        steps_completed_append={"step": "Validating", "status": "ok", "duration_ms": 100, "ts": "2026-04-26T00:00:00Z"},
        home=tmp_path,
    )
    install_state.update_state(
        "x",
        steps_completed_append={"step": "Cloning", "status": "ok", "duration_ms": 1200, "ts": "2026-04-26T00:00:01Z"},
        home=tmp_path,
    )
    state = install_state.read("x", home=tmp_path)
    assert len(state["steps_completed"]) == 2
    assert state["steps_completed"][0]["step"] == "Validating"
    assert state["steps_completed"][1]["step"] == "Cloning"


def test_update_state_returns_none_for_unknown(tmp_path):
    assert install_state.update_state("unknown", status="failed", home=tmp_path) is None


def test_append_event_writes_to_events_jsonl(tmp_path):
    install_state.create(install_id="evt-test", strategy="wheel", config={}, home=tmp_path)
    install_state.append_event(
        "evt-test", event_type="step",
        payload={"step": "X", "status": "in_progress", "ts": "..."},
        home=tmp_path,
    )
    events_path = tmp_path / ".nodeble-api" / "data" / "installs" / "evt-test" / "events.jsonl"
    lines = events_path.read_text().strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "step"
    assert parsed["data"]["step"] == "X"


def test_append_event_returns_false_for_unknown_install_id(tmp_path):
    """Defensive — unknown install_id should not error, just return False."""
    result = install_state.append_event(
        "never-created", event_type="step", payload={}, home=tmp_path,
    )
    assert result is False


def test_replay_events_yields_in_order(tmp_path):
    install_state.create(install_id="replay", strategy="wheel", config={}, home=tmp_path)
    install_state.append_event("replay", event_type="step", payload={"n": 1}, home=tmp_path)
    install_state.append_event("replay", event_type="step", payload={"n": 2}, home=tmp_path)
    install_state.append_event("replay", event_type="complete", payload={"status": "success"}, home=tmp_path)

    events = list(install_state.replay_events("replay", home=tmp_path))
    assert len(events) == 3
    assert events[0]["data"]["n"] == 1
    assert events[1]["data"]["n"] == 2
    assert events[2]["event"] == "complete"


def test_replay_events_empty_for_unknown(tmp_path):
    events = list(install_state.replay_events("unknown", home=tmp_path))
    assert events == []


def test_replay_events_skips_corrupt_lines(tmp_path):
    """A corrupt line in events.jsonl shouldn't crash replay."""
    install_dir = tmp_path / ".nodeble-api" / "data" / "installs" / "corrupt"
    install_dir.mkdir(parents=True)
    events_path = install_dir / "events.jsonl"
    events_path.write_text(
        '{"event": "step", "data": {"n": 1}}\n'
        'this is not json\n'
        '{"event": "step", "data": {"n": 2}}\n'
    )
    events = list(install_state.replay_events("corrupt", home=tmp_path))
    assert len(events) == 2  # corrupt line skipped


def test_cleanup_stale_running_marks_active_as_failed(tmp_path):
    """Simulate api-server restart: 'running' install is now stale."""
    install_state.create(install_id="stale-1", strategy="wheel", config={}, home=tmp_path)
    install_state.update_state("stale-1", status="running", home=tmp_path)
    install_state.create(install_id="terminal-1", strategy="ic", config={}, home=tmp_path)
    install_state.update_state(
        "terminal-1", status="success",
        completed_at="2026-04-26T00:00:00Z", home=tmp_path,
    )

    cleaned = install_state.cleanup_stale_running(home=tmp_path)
    assert "stale-1" in cleaned
    assert "terminal-1" not in cleaned

    # stale-1 now failed on disk
    state = install_state.read("stale-1", home=tmp_path)
    assert state["status"] == "failed"
    assert "restarted" in state["error"]


def test_cleanup_stale_running_appends_complete_event(tmp_path):
    install_state.create(install_id="boot-fail", strategy="wheel", config={}, home=tmp_path)
    install_state.update_state("boot-fail", status="running", home=tmp_path)

    install_state.cleanup_stale_running(home=tmp_path)

    events = list(install_state.replay_events("boot-fail", home=tmp_path))
    assert any(
        e["event"] == "complete" and e["data"]["status"] == "failed"
        for e in events
    )


def test_list_installs_returns_all_install_ids(tmp_path):
    install_state.create(install_id="a", strategy="wheel", config={}, home=tmp_path)
    install_state.create(install_id="b", strategy="ic", config={}, home=tmp_path)
    install_state.create(install_id="c", strategy="pmcc", config={}, home=tmp_path)

    ids = install_state.list_installs(home=tmp_path)
    assert sorted(ids) == ["a", "b", "c"]


def test_list_installs_empty_when_no_dir(tmp_path):
    """Fresh tmp_path has no installs/ dir — should return [] not crash."""
    assert install_state.list_installs(home=tmp_path) == []

"""Tests for audit.jsonl writer."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nodeble_api_server.audit import write_event

ET = ZoneInfo("America/New_York")


def test_write_event_creates_file_with_jsonl_line(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    write_event(
        strategy="ic",
        param_path="selection.put_delta_max",
        old_value=0.22,
        new_value=0.20,
        reason="test",
        result="success",
        path=path,
        now=datetime(2026, 4, 21, 15, 30, tzinfo=ET),
    )
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["strategy"] == "ic"
    assert event["param_path"] == "selection.put_delta_max"
    assert event["old_value"] == 0.22
    assert event["new_value"] == 0.20
    assert event["result"] == "success"
    assert event["actor"] == "desktop"
    assert event["error"] is None
    assert event["ts"].startswith("2026-04-21T15:30:00")


def test_write_event_appends_not_overwrites(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    for i in range(3):
        write_event(
            strategy="ic",
            param_path="x.y",
            old_value=i,
            new_value=i + 1,
            reason="",
            result="success",
            path=path,
        )
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    events = [json.loads(ln) for ln in lines]
    assert [e["old_value"] for e in events] == [0, 1, 2]


def test_write_event_failure_includes_error(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    write_event(
        strategy="wheel",
        param_path="selection.put_delta_max",
        old_value=0.20,
        new_value=0.80,
        reason="oops",
        result="validation_failed",
        error="value 0.8 above max 0.5",
        path=path,
    )
    event = json.loads(path.read_text().splitlines()[0])
    assert event["result"] == "validation_failed"
    assert event["error"] == "value 0.8 above max 0.5"


def test_write_event_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "nested" / "path" / "audit.jsonl"
    write_event(
        strategy="ic",
        param_path="x",
        old_value=1,
        new_value=2,
        reason="",
        result="success",
        path=path,
    )
    assert path.exists()


def test_write_event_handles_non_json_value(tmp_path: Path):
    """Values like sets or custom objects use the `default=str` fallback."""
    path = tmp_path / "audit.jsonl"
    write_event(
        strategy="ic",
        param_path="x",
        old_value={1, 2, 3},  # set isn't JSON-serializable
        new_value=[1, 2, 3],
        reason="",
        result="success",
        path=path,
    )
    event = json.loads(path.read_text().splitlines()[0])
    # Set gets stringified via default=str.
    assert isinstance(event["old_value"], str)
    assert event["new_value"] == [1, 2, 3]

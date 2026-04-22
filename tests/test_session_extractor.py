"""Tests for session_extractor.extract_sessions / extract_session_detail
and logs.read_recent_parsed_lines."""
from __future__ import annotations

from pathlib import Path

from nodeble_api_server.logs import read_recent_parsed_lines
from nodeble_api_server.session_extractor import (
    DEFAULT_GAP_SEC,
    extract_session_detail,
    extract_sessions,
)


# Helper: build a parsed-line dict the shape `parse_log_line` would return.
def _line(ts: str | None, level: str = "INFO", raw: str | None = None) -> dict:
    return {
        "ts": ts,
        "level": level,
        "module": "nodeble",
        "message": raw or (ts or "no-ts"),
        "raw": raw or f"{ts or ''} {level} {raw or 'msg'}".strip(),
    }


# ── extract_sessions: basic grouping ──────────────────────────────────────


def test_empty_input_returns_empty():
    assert extract_sessions([]) == []


def test_single_line_is_one_session():
    lines = [_line("2026-04-22T14:30:00-04:00")]
    sessions = extract_sessions(lines)
    assert len(sessions) == 1
    assert sessions[0].start_ts == "2026-04-22T14:30:00-04:00"
    assert sessions[0].end_ts == "2026-04-22T14:30:00-04:00"
    assert sessions[0].line_count == 1
    assert sessions[0].duration_sec == 0.0


def test_close_lines_grouped_into_one_session():
    lines = [
        _line("2026-04-22T14:30:00-04:00"),
        _line("2026-04-22T14:30:05-04:00"),
        _line("2026-04-22T14:30:12-04:00"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 1
    assert sessions[0].line_count == 3
    assert sessions[0].duration_sec == 12.0


def test_large_gap_splits_sessions():
    """>= DEFAULT_GAP_SEC → session boundary."""
    lines = [
        _line("2026-04-22T14:30:00-04:00"),
        _line("2026-04-22T14:30:05-04:00"),
        # 300s gap → new session (300 >= 180)
        _line("2026-04-22T14:35:05-04:00"),
        _line("2026-04-22T14:35:10-04:00"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 2
    # Returned newest-first.
    assert sessions[0].start_ts == "2026-04-22T14:35:05-04:00"
    assert sessions[1].start_ts == "2026-04-22T14:30:00-04:00"


def test_gap_just_under_threshold_stays_one_session():
    lines = [
        _line("2026-04-22T14:30:00-04:00"),
        # 179s later — just under 180s threshold.
        _line("2026-04-22T14:32:59-04:00"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 1


def test_custom_gap_sec():
    lines = [
        _line("2026-04-22T14:30:00-04:00"),
        _line("2026-04-22T14:30:10-04:00"),
    ]
    # 5s threshold: 10s gap → boundary.
    sessions = extract_sessions(lines, gap_sec=5)
    assert len(sessions) == 2


# ── no-ts line handling ───────────────────────────────────────────────────


def test_no_ts_line_attaches_to_current_session():
    """Traceback tail lines inherit the preceding session."""
    lines = [
        _line("2026-04-22T14:30:00-04:00", raw="first"),
        _line(None, level=None, raw="Traceback (most recent call last):"),
        _line(None, level=None, raw="  File ..."),
        _line("2026-04-22T14:30:05-04:00", raw="recovered"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 1
    assert sessions[0].line_count == 4
    # Start/end driven by ts-bearing lines only.
    assert sessions[0].start_ts == "2026-04-22T14:30:00-04:00"
    assert sessions[0].end_ts == "2026-04-22T14:30:05-04:00"


def test_leading_no_ts_lines_are_dropped():
    """No session open yet → nothing to attach to."""
    lines = [
        _line(None, level=None, raw="orphan traceback line"),
        _line(None, level=None, raw="another orphan"),
        _line("2026-04-22T14:30:00-04:00", raw="first real"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 1
    assert sessions[0].line_count == 1


def test_no_ts_lines_between_sessions_attach_to_earlier_one():
    """No-ts lines after a session and before the next cron:
    they belong with the session that produced them (traceback tail)."""
    lines = [
        _line("2026-04-22T14:30:00-04:00", raw="session 1 line 1"),
        _line(None, raw="tb line"),
        # 300s gap from the LAST ts-bearing line (14:30:00) → new session.
        _line("2026-04-22T14:35:00-04:00", raw="session 2 line 1"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 2
    # The no-ts line belongs to the older session.
    oldest = sessions[-1]
    assert oldest.line_count == 2


# ── level_counts ──────────────────────────────────────────────────────────


def test_level_counts_aggregate_correctly():
    lines = [
        _line("2026-04-22T14:30:00-04:00", level="INFO"),
        _line("2026-04-22T14:30:01-04:00", level="INFO"),
        _line("2026-04-22T14:30:02-04:00", level="WARNING"),
        _line("2026-04-22T14:30:03-04:00", level="ERROR"),
        _line("2026-04-22T14:30:04-04:00", level="INFO"),
    ]
    sessions = extract_sessions(lines)
    assert sessions[0].level_counts == {"INFO": 3, "WARNING": 1, "ERROR": 1}


def test_level_counts_includes_no_ts_lines():
    """No-ts lines contribute to line_count and level_counts too."""
    lines = [
        _line("2026-04-22T14:30:00-04:00", level="ERROR"),
        _line(None, level=None, raw="tb"),
    ]
    sessions = extract_sessions(lines)
    assert sessions[0].line_count == 2
    assert sessions[0].level_counts == {"ERROR": 1, "UNKNOWN": 1}


# ── preview_head / preview_tail ───────────────────────────────────────────


def test_preview_head_tail_short_session_overlaps():
    """≤ 6 lines means head/tail overlap — accepted."""
    lines = [
        _line("2026-04-22T14:30:00-04:00", raw="a"),
        _line("2026-04-22T14:30:01-04:00", raw="b"),
        _line("2026-04-22T14:30:02-04:00", raw="c"),
        _line("2026-04-22T14:30:03-04:00", raw="d"),
    ]
    sessions = extract_sessions(lines)
    assert sessions[0].preview_head == ["a", "b", "c"]
    assert sessions[0].preview_tail == ["b", "c", "d"]


def test_preview_head_tail_long_session_distinct():
    lines = [
        _line(f"2026-04-22T14:30:{i:02d}-04:00", raw=f"line-{i}")
        for i in range(10)
    ]
    sessions = extract_sessions(lines)
    assert sessions[0].preview_head == ["line-0", "line-1", "line-2"]
    assert sessions[0].preview_tail == ["line-7", "line-8", "line-9"]


# ── before_ts / limit ─────────────────────────────────────────────────────


def test_before_ts_filters_sessions():
    lines = [
        _line("2026-04-22T14:00:00-04:00"),
        _line("2026-04-22T14:10:00-04:00"),  # 600s > 180s → session boundary
        _line("2026-04-22T14:20:00-04:00"),  # another boundary
    ]
    all_sessions = extract_sessions(lines)
    assert len(all_sessions) == 3

    # before_ts strict-less-than: sessions with start_ts < cutoff.
    filtered = extract_sessions(lines, before_ts="2026-04-22T14:20:00-04:00")
    # Should exclude the 14:20 session.
    assert len(filtered) == 2
    assert filtered[0].start_ts == "2026-04-22T14:10:00-04:00"


def test_limit_clamps_session_count():
    # 5 sessions, 600s apart each.
    lines = [_line(f"2026-04-22T14:{i * 10:02d}:00-04:00") for i in range(5)]
    sessions = extract_sessions(lines, limit=2)
    assert len(sessions) == 2
    # Newest 2.
    assert sessions[0].start_ts == "2026-04-22T14:40:00-04:00"
    assert sessions[1].start_ts == "2026-04-22T14:30:00-04:00"


def test_limit_none_returns_all():
    lines = [_line(f"2026-04-22T14:{i * 10:02d}:00-04:00") for i in range(5)]
    assert len(extract_sessions(lines, limit=None)) == 5


# ── extract_session_detail ────────────────────────────────────────────────


def test_detail_returns_in_range_lines():
    lines = [
        _line("2026-04-22T14:30:00-04:00", raw="before"),
        _line("2026-04-22T14:35:00-04:00", raw="session-start"),
        _line("2026-04-22T14:35:10-04:00", raw="session-mid"),
        _line("2026-04-22T14:35:20-04:00", raw="session-end"),
        _line("2026-04-22T14:40:00-04:00", raw="after"),
    ]
    out = extract_session_detail(
        lines,
        start_ts="2026-04-22T14:35:00-04:00",
        end_ts="2026-04-22T14:35:20-04:00",
    )
    raws = [ln["raw"] for ln in out]
    assert raws == ["session-start", "session-mid", "session-end"]


def test_detail_boundaries_are_inclusive():
    lines = [
        _line("2026-04-22T14:35:00-04:00", raw="at-start"),
        _line("2026-04-22T14:35:20-04:00", raw="at-end"),
    ]
    out = extract_session_detail(
        lines,
        start_ts="2026-04-22T14:35:00-04:00",
        end_ts="2026-04-22T14:35:20-04:00",
    )
    assert len(out) == 2


def test_detail_no_ts_line_inherits_prev_in_range():
    """Traceback lines after an in-range header come through."""
    lines = [
        _line("2026-04-22T14:30:00-04:00", raw="before"),
        _line("2026-04-22T14:35:00-04:00", raw="in-range"),
        _line(None, raw="tb line 1"),
        _line(None, raw="tb line 2"),
        _line("2026-04-22T14:40:00-04:00", raw="after"),
    ]
    out = extract_session_detail(
        lines,
        start_ts="2026-04-22T14:35:00-04:00",
        end_ts="2026-04-22T14:35:00-04:00",
    )
    raws = [ln["raw"] for ln in out]
    assert raws == ["in-range", "tb line 1", "tb line 2"]


def test_detail_no_ts_line_after_out_of_range_is_dropped():
    lines = [
        _line("2026-04-22T14:30:00-04:00", raw="out-of-range"),
        _line(None, raw="tb after out"),
        _line("2026-04-22T14:35:00-04:00", raw="in-range"),
    ]
    out = extract_session_detail(
        lines,
        start_ts="2026-04-22T14:35:00-04:00",
        end_ts="2026-04-22T14:35:00-04:00",
    )
    raws = [ln["raw"] for ln in out]
    # "tb after out" was attached to an out-of-range line → excluded.
    assert raws == ["in-range"]


def test_detail_leading_no_ts_lines_dropped():
    lines = [
        _line(None, raw="orphan"),
        _line("2026-04-22T14:35:00-04:00", raw="in-range"),
    ]
    out = extract_session_detail(
        lines,
        start_ts="2026-04-22T14:35:00-04:00",
        end_ts="2026-04-22T14:35:00-04:00",
    )
    assert [ln["raw"] for ln in out] == ["in-range"]


def test_detail_empty_input():
    assert extract_session_detail([], "a", "z") == []


# ── read_recent_parsed_lines ──────────────────────────────────────────────


def test_read_recent_missing_file(tmp_path: Path):
    assert read_recent_parsed_lines(tmp_path / "nope.log") == []


def test_read_recent_small_file(tmp_path: Path):
    path = tmp_path / "x.log"
    path.write_text(
        "\n".join(
            [
                "2026-04-22 14:30:00 nodeble INFO a",
                "2026-04-22 14:30:01 nodeble INFO b",
                "2026-04-22 14:30:02 nodeble INFO c",
            ]
        )
        + "\n"
    )
    lines = read_recent_parsed_lines(path)
    assert len(lines) == 3
    assert lines[0]["message"] == "a"
    assert lines[-1]["message"] == "c"


def test_read_recent_limits_to_max_lines(tmp_path: Path):
    path = tmp_path / "big.log"
    path.write_text(
        "\n".join([f"2026-04-22 14:30:00 nodeble INFO line-{i}" for i in range(100)])
        + "\n"
    )
    lines = read_recent_parsed_lines(path, max_lines=10)
    assert len(lines) == 10
    assert lines[0]["message"] == "line-90"
    assert lines[-1]["message"] == "line-99"


def test_read_recent_drops_partial_first_line_when_chunk_mid_file(tmp_path: Path):
    path = tmp_path / "big.log"
    # Build a file larger than max_bytes so the read starts mid-line.
    body = "\n".join(
        [f"2026-04-22 14:30:00 nodeble INFO line-{i}" for i in range(5000)]
    )
    path.write_text(body + "\n")
    # Tiny byte budget forces chunk_start > 0.
    lines = read_recent_parsed_lines(path, max_lines=999999, max_bytes=200)
    # First element should be a fully formed line (no mid-line fragment).
    assert lines[0]["raw"].startswith("2026-04-22 14:30:00 nodeble INFO line-")


# ── integration: sessionize → detail roundtrip ───────────────────────────


def test_sessionize_and_detail_roundtrip():
    """End-to-end: real-ish log → list sessions → fetch detail of one."""
    lines = [
        # Session 1
        _line("2026-04-22T14:00:00-04:00", raw="s1-1"),
        _line("2026-04-22T14:00:03-04:00", raw="s1-2"),
        _line("2026-04-22T14:00:05-04:00", raw="s1-3"),
        # Gap
        _line("2026-04-22T14:10:00-04:00", raw="s2-1"),
        _line("2026-04-22T14:10:02-04:00", raw="s2-2"),
    ]
    sessions = extract_sessions(lines)
    assert len(sessions) == 2

    # Take the older session's window and fetch detail.
    older = sessions[-1]
    detail = extract_session_detail(lines, older.start_ts, older.end_ts)
    assert [d["raw"] for d in detail] == ["s1-1", "s1-2", "s1-3"]

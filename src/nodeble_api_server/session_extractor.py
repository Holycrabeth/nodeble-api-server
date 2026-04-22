"""Sessionize parsed log lines into "cron runs" for the History tab.

A session = a contiguous group of log lines where the inter-line gap is
< `gap_sec` seconds. Groups one cron run's output (scan + manage + notify)
into a single entry without needing per-strategy parsers, so it works
uniformly across all 9 strategies (IC/Wheel's 5-min cron vs Calendar/
Ironbutterfly's slower cadence).

Pure functions only. I/O lives in logs.py (read_recent_parsed_lines).

Why 180s default gap:
- IC/Wheel/PMCC/DirectionalSpread cron every 5 min (300s). A single run
  takes 1-10 s, so inter-cron gaps are at least ~290 s. 180 s safely
  separates distinct runs.
- Intra-run gaps (waiting on broker API, sleep between manage+notify)
  are typically < 60 s, well under 180 s, so we don't wrongly split.
- Strategies with slower crons (Calendar = 10+ min) only become easier
  to sessionize — smaller gap threshold never merges them incorrectly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DEFAULT_GAP_SEC = 180  # 3 minutes — see module docstring for rationale.


@dataclass
class SessionSummary:
    """One "cron run" as shown on the 运行历史 card.

    `start_ts` / `end_ts` are the ISO timestamps of the first/last log
    line in the session (lines without ts don't move these boundaries).
    Together they form the session's unique key — the detail endpoint
    takes them as query params to fetch the full line list.

    `preview_head` / `preview_tail` are raw log-line strings (not parsed
    dicts) — the collapsed accordion row shows them as a quick glance.
    For sessions with ≤ 6 lines these will overlap; that's acceptable
    (signals "this is all of it" to the viewer).
    """
    start_ts: str
    end_ts: str
    duration_sec: float
    line_count: int
    level_counts: dict[str, int] = field(default_factory=dict)
    preview_head: list[str] = field(default_factory=list)
    preview_tail: list[str] = field(default_factory=list)


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 string → datetime; None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _summarize(lines: list[dict[str, Any]]) -> SessionSummary | None:
    """Build a SessionSummary from a session's parsed lines. Returns
    None if no line has a parseable timestamp (defensive — the caller
    shouldn't feed such a session in)."""
    ts_lines = [ln for ln in lines if ln.get("ts")]
    if not ts_lines:
        return None

    start_ts = str(ts_lines[0]["ts"])
    end_ts = str(ts_lines[-1]["ts"])
    start_dt = _parse_iso(start_ts)
    end_dt = _parse_iso(end_ts)
    duration = (end_dt - start_dt).total_seconds() if start_dt and end_dt else 0.0

    level_counts: dict[str, int] = {}
    for ln in lines:
        lvl = ln.get("level") or "UNKNOWN"
        level_counts[lvl] = level_counts.get(lvl, 0) + 1

    raws = [str(ln.get("raw", "")) for ln in lines]

    return SessionSummary(
        start_ts=start_ts,
        end_ts=end_ts,
        duration_sec=duration,
        line_count=len(lines),
        level_counts=level_counts,
        preview_head=raws[:3],
        preview_tail=raws[-3:],
    )


def extract_sessions(
    parsed_lines: list[dict[str, Any]],
    gap_sec: int = DEFAULT_GAP_SEC,
    limit: int | None = None,
    before_ts: str | None = None,
) -> list[SessionSummary]:
    """Group `parsed_lines` (chronological, oldest first) into sessions.

    Returns newest-first.

    Lines without a parseable `ts` field attach to the currently-open
    session (multi-line tracebacks etc.). If the input begins with no-ts
    lines (nothing to attach to), they're dropped.

    `before_ts`: only sessions with `start_ts < before_ts` are returned
    (strict less-than, matches audit_reader convention). Works on ISO-8601
    lexicographic sort — callers must pass timestamps from the same
    timezone as the log lines (true for our case: all log parsers
    normalize via normalize_timestamp which preserves the offset).

    `limit`: clamp returned list to N most-recent sessions; None = all.
    """
    if not parsed_lines:
        return []

    # Walk lines and build raw session buckets.
    buckets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_dt: datetime | None = None

    for line in parsed_lines:
        ts = line.get("ts")
        dt = _parse_iso(ts) if ts else None

        if dt is None:
            # No timestamp → attach to whatever session is open. If
            # nothing is open yet (file begins with a traceback-looking
            # line), drop it silently.
            if current:
                current.append(line)
            continue

        if last_dt is None or (dt - last_dt).total_seconds() >= gap_sec:
            # New session boundary. Flush the open bucket first.
            if current:
                buckets.append(current)
            current = [line]
        else:
            current.append(line)
        last_dt = dt

    if current:
        buckets.append(current)

    summaries: list[SessionSummary] = []
    for bucket in buckets:
        summary = _summarize(bucket)
        if summary is not None:
            summaries.append(summary)

    # Newest first for display.
    summaries.reverse()

    # Pagination cursor.
    if before_ts is not None:
        summaries = [s for s in summaries if s.start_ts < before_ts]

    if limit is not None:
        summaries = summaries[:limit]

    return summaries


def extract_session_detail(
    parsed_lines: list[dict[str, Any]],
    start_ts: str,
    end_ts: str,
) -> list[dict[str, Any]]:
    """Return parsed lines inside a session window.

    Inclusion rules:
    - A line with a ts field is included iff `start_ts <= ts <= end_ts`
      (inclusive on both sides — the session's boundary lines must show
      up in detail).
    - A line with NO ts inherits the inclusion of the nearest preceding
      line that did have a ts. This keeps multi-line tracebacks attached
      to their header line even when they're at the tail of the session.
    - Lines with no ts at the very start of `parsed_lines` (before any
      ts-bearing line) are dropped — same behavior as extract_sessions.
    """
    result: list[dict[str, Any]] = []
    last_in_range = False
    for line in parsed_lines:
        ts = line.get("ts")
        if ts is None:
            if last_in_range:
                result.append(line)
            continue
        ts_str = str(ts)
        if start_ts <= ts_str <= end_ts:
            result.append(line)
            last_in_range = True
        else:
            last_in_range = False
    return result

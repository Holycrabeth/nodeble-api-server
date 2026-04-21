"""Incremental log reader + line parser for /api/v1/strategies/{id}/logs.

Two public functions:
- `tail_bytes(path, cursor, limit)` — byte-offset cursor read. First fetch
  (cursor=None) seeks to EOF and reads the last `limit` lines; subsequent
  fetches seek to `cursor` and read to EOF. Rotate is detected when the
  file size shrinks below the cursor.
- `parse_log_line(raw)` — tries JSON → Python-stdlib text regex → fallback
  to raw-only. Returns a dict with ts/level/module/message/raw; any field
  that can't be extracted is None (raw is always set).

Neither function touches the strategy source files; both are read-only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nodeble_api_server.state_reader import normalize_timestamp

# Chunk size for the initial reverse-read. Large enough to hold ~500 typical
# log lines but small enough that we don't pull MBs for a 200-line tail.
_INITIAL_CHUNK_BYTES = 100 * 1024  # 100 KB

# ── Line parser ────────────────────────────────────────────────────────────

# Python stdlib format observed on ic/wheel/pmcc:
#   "2026-04-20 14:30:03,746 nodeble INFO message here"
_PY_TEXT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+"
    r"(?P<module>[\w.\-]+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+"
    r"(?P<message>.*)$"
)

# Bracketed variant — chief designer's documented format. Observed nowhere
# in the 9 strategies but kept as a future-proof fallback:
#   "2026-04-20 14:30:03 [INFO] nodeble.ic: message"
_PY_BRACKET_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+"
    r"\[(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]\s+"
    r"(?P<module>[\w.\-]+)\s*:\s*"
    r"(?P<message>.*)$"
)


def _parse_json_line(raw: str) -> dict[str, Any] | None:
    """Extract ts/level/module/message from a JSON log record. Covers the
    shape used by calendar/ironbutterfly/straddle/strangle which emit
    `{"ts", "lvl", "subsys", "msg"}` via a structured logger."""
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    ts_raw = obj.get("ts") or obj.get("timestamp") or obj.get("time")
    level_raw = obj.get("lvl") or obj.get("level") or obj.get("levelname")
    module_raw = (
        obj.get("subsys")
        or obj.get("module")
        or obj.get("logger")
        or obj.get("name")
    )
    msg_raw = obj.get("msg") or obj.get("message")
    return {
        "ts": normalize_timestamp(ts_raw) if ts_raw else None,
        "level": str(level_raw).upper() if level_raw else None,
        "module": str(module_raw) if module_raw else None,
        "message": str(msg_raw) if msg_raw is not None else None,
        "raw": raw,
    }


def _parse_text_line(raw: str, regex: re.Pattern[str]) -> dict[str, Any] | None:
    m = regex.match(raw)
    if not m:
        return None
    ts = m.group("ts")
    # Python stdlib writes "YYYY-MM-DD HH:MM:SS,mmm" — replace the comma so
    # fromisoformat accepts it (comma is a valid fractional separator in
    # ISO 8601 but Python rejects it in fromisoformat pre-3.11 edge cases).
    ts_normal = ts.replace(",", ".") if ts else None
    return {
        "ts": normalize_timestamp(ts_normal) if ts_normal else None,
        "level": m.group("level").upper(),
        "module": m.group("module"),
        "message": m.group("message"),
        "raw": raw,
    }


def parse_log_line(raw: str) -> dict[str, Any]:
    """Parse one log line into a structured dict. Return `raw` alone
    (other fields None) when no format matches — never drops a line."""
    raw = raw.rstrip("\r\n")
    stripped = raw.lstrip()

    parsed = _parse_json_line(stripped)
    if parsed is not None:
        return parsed

    parsed = _parse_text_line(raw, _PY_TEXT_RE)
    if parsed is not None:
        return parsed

    parsed = _parse_text_line(raw, _PY_BRACKET_RE)
    if parsed is not None:
        return parsed

    return {
        "ts": None,
        "level": None,
        "module": None,
        "message": None,
        "raw": raw,
    }


# ── File tailer ────────────────────────────────────────────────────────────


def _decode(data: bytes) -> str:
    """Decode bytes to str with replacement for any invalid sequences."""
    return data.decode("utf-8", errors="replace")


def tail_bytes(
    path: Path,
    cursor: int | None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return up to `limit` lines from `path`, using byte-offset cursoring.

    Shape: `{"lines": [...], "cursor": int, "truncated": bool}`
    - Missing file: empty lines, cursor 0, truncated False.
    - cursor is None: initial load — reverse-read the last `_INITIAL_CHUNK_BYTES`
      and keep the final `limit` complete lines; cursor becomes EOF.
    - cursor valid: seek + read to EOF; all new lines returned; cursor
      becomes new EOF.
    - cursor exceeds current size: file rotated → fall back to initial
      load path and mark truncated=True so the client knows to reset.
    """
    if not path.exists():
        return {"lines": [], "cursor": 0, "truncated": False}

    try:
        size = path.stat().st_size
    except OSError:
        return {"lines": [], "cursor": 0, "truncated": False}

    truncated = cursor is not None and cursor > size
    initial = cursor is None or truncated

    with open(path, "rb") as f:
        if initial:
            chunk_start = max(0, size - _INITIAL_CHUNK_BYTES)
            f.seek(chunk_start)
            chunk = f.read()
            text = _decode(chunk)
            # If we started mid-line (not at file start), drop the first
            # (partial) line to avoid showing a fragment.
            split = text.splitlines()
            if chunk_start > 0 and split:
                split = split[1:]
            tail_lines = split[-limit:] if limit > 0 else split
            new_cursor = size
        else:
            assert cursor is not None  # narrow for type-checker
            f.seek(cursor)
            chunk = f.read()
            text = _decode(chunk)
            split = text.splitlines()
            # If the last byte isn't a newline the tail is incomplete —
            # the remaining partial line will come back on the next poll
            # via the new cursor, so we exclude it now. Best effort: if
            # the chunk ends with \n, every line is complete; otherwise
            # the last string is partial.
            if chunk and not chunk.endswith(b"\n") and split:
                # Adjust the cursor back so the partial line re-ships
                # fully next time. Length of the trailing partial:
                trailing = split[-1].encode("utf-8", errors="replace")
                new_cursor = size - len(trailing)
                split = split[:-1]
            else:
                new_cursor = size
            tail_lines = split

    parsed = [parse_log_line(line) for line in tail_lines]
    return {
        "lines": parsed,
        "cursor": new_cursor,
        "truncated": truncated,
    }

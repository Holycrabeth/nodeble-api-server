"""Read-side companion to `audit.py`.

Parses the append-only audit.jsonl that `audit.write_event` produces and
returns filtered / paginated event lists for the desktop app's History
tab. No caching (events are small, file grows slowly) and no streaming
reverse-read in v1 — the whole file is loaded and sorted in memory.
Once audit logs reach MB scale or query latency becomes noticeable
we'll either index or move to a proper store. Until then, this is the
simplest thing that could possibly work.

Malformed JSON lines are silently skipped — audit is a legal-grade
record and we never mutate the file, even to prune garbage. Any line
that `json.loads` rejects is assumed to be a write-tear from a past
crash or a hand-edit; skipping preserves forward progress without
losing context.
"""
from __future__ import annotations

import json
from pathlib import Path


def read_audit_entries(
    path: Path,
    strategy: str | None = None,
    limit: int = 50,
    before_ts: str | None = None,
) -> list[dict]:
    """Return audit events in reverse chronological order (newest first).

    - Missing file → empty list, not an exception.
    - Lines that fail `json.loads` or lack a `ts` field are skipped.
    - `strategy` filter: exact match on the `strategy` field.
    - `before_ts` filter: only entries with `ts < before_ts`. Relies on
      lexicographic ordering of ISO 8601 strings — holds whenever all
      entries share the same timezone (they do; audit.py writes ET).
    - `limit <= 0` returns an empty list.
    """
    if limit <= 0:
        return []
    if not path.exists():
        return []

    entries: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("ts")
                if not isinstance(ts, str):
                    continue
                if strategy is not None and obj.get("strategy") != strategy:
                    continue
                if before_ts is not None and ts >= before_ts:
                    continue
                entries.append(obj)
    except OSError:
        return []

    # Reverse chronological — newest first. Stable sort on ts key.
    entries.sort(key=lambda e: e["ts"], reverse=True)
    return entries[:limit]

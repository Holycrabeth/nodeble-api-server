"""Append-only audit log for config edits and other sensitive actions.

JSONL at ~/.nodeble-api/audit/audit.jsonl. Each line is a self-contained
event — no rotation in v1 (M1.h scope); will add size-based rotation
once files get to GB scale. Fsync on every write so a hard crash after
writing a config change cannot lose the audit record.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SERVER_TZ = ZoneInfo("America/New_York")
_DEFAULT_AUDIT_PATH = Path("~/.nodeble-api/audit/audit.jsonl").expanduser()


def audit_path() -> Path:
    """Resolves at call time so tests can monkeypatch HOME."""
    return _DEFAULT_AUDIT_PATH


def write_event(
    strategy: str,
    param_path: str,
    old_value: Any,
    new_value: Any,
    reason: str,
    result: str,
    error: str | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Append one audit event. `result` is one of:
    success / validation_failed / write_failed / timeout / shim_error.
    """
    path = path or audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    ts = (now or datetime.now(_SERVER_TZ)).isoformat()
    event = {
        "ts": ts,
        "actor": "desktop",
        "strategy": strategy,
        "param_path": param_path,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
        "result": result,
        "error": error,
    }
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"

    # fsync ensures the audit record hits the platter before we ack the
    # client. The YAML write is already fsync'd by os.replace-style writers
    # in the shims; this closes the remaining loss window.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())

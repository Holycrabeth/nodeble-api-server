"""Global token-redaction for the logging subsystem.

Context:
- `uvicorn.access=False` disables the HTTP access log, but WebSocket
  handshake events emit through `uvicorn.error` at INFO level, which
  prints the full request line INCLUDING the `?token=...` query string.
- We can't rely on silencing specific loggers because uvicorn installs
  its log config after our module imports, and any future middleware
  could introduce another handler that re-leaks the value.

Fix:
- Install a process-wide LogRecordFactory that scans every record's
  formatted message and replaces `token=<value>` with `token=<redacted>`.
- This runs BEFORE any handler sees the record, so every log destination
  (stdout, file, syslog) is covered without per-logger wiring.
"""
from __future__ import annotations

import logging
import re

_TOKEN_PATTERN = re.compile(r"(token=)([^&\s\"'<>]+)")
_REDACTED_VALUE = "<redacted>"


def redact_tokens(text: str) -> str:
    """Replace `token=<value>` with `token=<redacted>` in a string. Pure
    function, used by both the log-record factory and direct callers
    (e.g., exception messages built before logging)."""
    return _TOKEN_PATTERN.sub(rf"\1{_REDACTED_VALUE}", text)


def install_token_redaction() -> None:
    """Wrap logging.getLogRecordFactory so every LogRecord emitted by any
    logger goes through redaction before handlers format it. Idempotent —
    calling it twice is harmless (the second wrap passes through records
    that are already redacted)."""
    original = logging.getLogRecordFactory()

    def sanitizing_factory(*args, **kwargs):
        record = original(*args, **kwargs)
        try:
            rendered = record.getMessage()
        except Exception:
            return record
        if "token=" not in rendered:
            return record
        # Replace the format message with the fully-rendered + redacted
        # text and clear args so no re-substitution happens downstream.
        record.msg = redact_tokens(rendered)
        record.args = None
        return record

    logging.setLogRecordFactory(sanitizing_factory)

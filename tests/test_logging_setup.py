"""Unit tests for token redaction in the global LogRecord factory."""
from __future__ import annotations

import io
import logging

import pytest

from nodeble_api_server.logging_setup import (
    install_token_redaction,
    redact_tokens,
)


# ── Pure function tests ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            'GET /ws?token=abc123 HTTP/1.1',
            'GET /ws?token=<redacted> HTTP/1.1',
        ),
        (
            '"WebSocket /api/v1/ws?token=a3aae14a-3400-4ca6-9cec-1835167afeaf" [accepted]',
            '"WebSocket /api/v1/ws?token=<redacted>" [accepted]',
        ),
        (
            "foo=1&token=secret&bar=2",
            "foo=1&token=<redacted>&bar=2",
        ),
        (
            "token=xyz",
            "token=<redacted>",
        ),
        (
            "nothing sensitive here",
            "nothing sensitive here",
        ),
        (
            "token= ",  # empty-ish values still need something to redact
            "token= ",  # nothing after the =, nothing to sub
        ),
    ],
)
def test_redact_tokens_pure(raw: str, expected: str):
    assert redact_tokens(raw) == expected


def test_redact_leaves_other_query_params_intact():
    line = "/api/v1/x?foo=bar&token=abc123&baz=quux"
    out = redact_tokens(line)
    assert "token=<redacted>" in out
    assert "foo=bar" in out
    assert "baz=quux" in out


# ── LogRecordFactory integration ──────────────────────────────────────────


@pytest.fixture
def captured_log():
    """Capture log output through the global logging module using our
    factory. Restores factory on teardown so other tests aren't affected."""
    original_factory = logging.getLogRecordFactory()
    install_token_redaction()

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    # Use a named throwaway logger so we don't pollute root.
    log = logging.getLogger("nodeble_api_server.test.redact")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False

    yield log, buf

    log.removeHandler(handler)
    logging.setLogRecordFactory(original_factory)


def test_factory_redacts_plain_message(captured_log):
    log, buf = captured_log
    log.info("hit /api/v1/ws?token=s3kr1t OK")
    assert "token=<redacted>" in buf.getvalue()
    assert "s3kr1t" not in buf.getvalue()


def test_factory_redacts_args_substitution(captured_log):
    """uvicorn-style formatting: `logger.info("%s - \"WebSocket %s\"", addr, path)`.
    The path string contains the token; factory must see the final text."""
    log, buf = captured_log
    log.info(
        '%s - "WebSocket %s" [accepted]',
        "192.168.1.6:12345",
        "/api/v1/ws?token=a3aae14a-3400-4ca6-9cec-1835167afeaf",
    )
    rendered = buf.getvalue()
    assert "token=<redacted>" in rendered
    assert "a3aae14a" not in rendered
    assert "192.168.1.6" in rendered  # non-token content preserved


def test_factory_passes_through_messages_without_token(captured_log):
    log, buf = captured_log
    log.info("server startup complete")
    assert buf.getvalue().strip() == "server startup complete"


def test_install_is_idempotent(captured_log):
    log, buf = captured_log
    # Call again — second factory wraps the first. Redaction must still apply.
    install_token_redaction()
    log.info("visit /x?token=yyy now")
    assert "token=<redacted>" in buf.getvalue()
    assert "yyy" not in buf.getvalue()

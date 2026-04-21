"""Tests for /api/v1/strategies/{id}/logs + logs.tail_bytes + parse_log_line."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.logs import parse_log_line, tail_bytes


VALID_TOKEN = "logs-test-token"


# ── tail_bytes unit tests ─────────────────────────────────────────────────


def test_tail_missing_file(tmp_path: Path):
    out = tail_bytes(tmp_path / "nope.log", cursor=None, limit=10)
    assert out == {"lines": [], "cursor": 0, "truncated": False}


def test_tail_initial_small_file(tmp_path: Path):
    path = tmp_path / "x.log"
    lines = [f"2026-04-21 14:30:0{i} nodeble INFO line {i}" for i in range(5)]
    path.write_text("\n".join(lines) + "\n")

    out = tail_bytes(path, cursor=None, limit=10)
    assert len(out["lines"]) == 5
    assert out["cursor"] == path.stat().st_size
    assert out["truncated"] is False
    assert out["lines"][0]["message"] == "line 0"
    assert out["lines"][-1]["message"] == "line 4"


def test_tail_initial_respects_limit_for_large_file(tmp_path: Path):
    path = tmp_path / "big.log"
    lines = [f"2026-04-21 14:30:00 nodeble INFO line {i}" for i in range(1000)]
    path.write_text("\n".join(lines) + "\n")

    out = tail_bytes(path, cursor=None, limit=10)
    assert len(out["lines"]) == 10
    # Last 10 of 1000 — line 990..999
    assert out["lines"][0]["message"] == "line 990"
    assert out["lines"][-1]["message"] == "line 999"


def test_tail_incremental_append(tmp_path: Path):
    path = tmp_path / "x.log"
    path.write_text("2026-04-21 14:30:00 nodeble INFO a\n")

    first = tail_bytes(path, cursor=None, limit=10)
    assert len(first["lines"]) == 1

    # Append two lines
    with open(path, "a") as f:
        f.write("2026-04-21 14:30:01 nodeble INFO b\n")
        f.write("2026-04-21 14:30:02 nodeble INFO c\n")

    second = tail_bytes(path, cursor=first["cursor"], limit=10)
    assert len(second["lines"]) == 2
    assert [ln["message"] for ln in second["lines"]] == ["b", "c"]
    assert second["cursor"] == path.stat().st_size
    assert second["truncated"] is False


def test_tail_detects_rotate(tmp_path: Path):
    path = tmp_path / "x.log"
    path.write_text("\n".join([f"line {i}" for i in range(50)]) + "\n")
    first = tail_bytes(path, cursor=None, limit=200)
    first_cursor = first["cursor"]

    # Simulate rotation: file shrinks (e.g. logrotate truncated + new content)
    path.write_text("fresh line 1\nfresh line 2\n")
    out = tail_bytes(path, cursor=first_cursor, limit=10)
    assert out["truncated"] is True
    assert len(out["lines"]) == 2
    assert out["lines"][-1]["raw"] == "fresh line 2"


def test_tail_handles_non_utf8_bytes(tmp_path: Path):
    path = tmp_path / "bad.log"
    # 0xFF is invalid in UTF-8; should be replaced, not raise.
    with open(path, "wb") as f:
        f.write(b"2026-04-21 14:30:00 nodeble INFO hello\n")
        f.write(b"2026-04-21 14:30:01 nodeble INFO broken \xff bytes\n")

    out = tail_bytes(path, cursor=None, limit=10)
    assert len(out["lines"]) == 2
    # Replacement character (U+FFFD) substituted for invalid byte.
    assert "\ufffd" in out["lines"][1]["raw"]


def test_tail_incremental_preserves_partial_trailing_line(tmp_path: Path):
    """If the last byte of the file isn't a newline (write in progress),
    the partial line must NOT be returned — it'll come back complete on
    the next poll when the writer finishes the line."""
    path = tmp_path / "x.log"
    path.write_text("2026-04-21 14:30:00 nodeble INFO complete\n")
    first = tail_bytes(path, cursor=None, limit=10)

    # Append a complete line + a partial (no trailing newline)
    with open(path, "a") as f:
        f.write("2026-04-21 14:30:01 nodeble INFO also complete\n")
        f.write("2026-04-21 14:30:02 nodeble INFO partial...")

    out = tail_bytes(path, cursor=first["cursor"], limit=10)
    # Only the complete line comes through. Cursor is set so the partial
    # will re-ship whole next time.
    assert len(out["lines"]) == 1
    assert out["lines"][0]["message"] == "also complete"
    assert out["cursor"] < path.stat().st_size


# ── parse_log_line unit tests ─────────────────────────────────────────────


def test_parse_python_text_format_with_ms_comma():
    """Real format from ic/wheel/pmcc: 'TS,ms MODULE LEVEL MSG'."""
    raw = "2026-04-20 14:30:03,746 nodeble.core.state INFO Spread state saved: 14 positions"
    p = parse_log_line(raw)
    assert p["level"] == "INFO"
    assert p["module"] == "nodeble.core.state"
    assert p["message"] == "Spread state saved: 14 positions"
    assert p["ts"] is not None
    assert p["ts"].startswith("2026-04-20T14:30:03")
    assert p["raw"] == raw


def test_parse_python_text_format_no_ms():
    raw = "2026-04-21 09:15:00 nodeble WARNING low IV rank, skip"
    p = parse_log_line(raw)
    assert p["level"] == "WARNING"
    assert p["module"] == "nodeble"
    assert p["message"] == "low IV rank, skip"


def test_parse_bracket_format():
    """Chief-designer-documented format with brackets — future-proofing."""
    raw = "2026-04-21 14:30:05 [INFO] nodeble.ic.scanner: Found 5 candidates"
    p = parse_log_line(raw)
    assert p["level"] == "INFO"
    assert p["module"] == "nodeble.ic.scanner"
    assert p["message"] == "Found 5 candidates"


def test_parse_json_line():
    """JSON shape from calendar/ironbutterfly/straddle/strangle."""
    raw = '{"ts": "2026-04-17T02:05:08.943-04:00", "lvl": "INFO", "subsys": "bot", "msg": "Starting"}'
    p = parse_log_line(raw)
    assert p["level"] == "INFO"
    assert p["module"] == "bot"
    assert p["message"] == "Starting"
    assert p["ts"] is not None
    assert p["ts"].startswith("2026-04-17T02:05:08")
    assert p["raw"] == raw


def test_parse_json_line_alternate_keys():
    raw = '{"timestamp": "2026-04-17T02:05:08-04:00", "level": "ERROR", "logger": "foo", "message": "broken"}'
    p = parse_log_line(raw)
    assert p["level"] == "ERROR"
    assert p["module"] == "foo"
    assert p["message"] == "broken"


def test_parse_unstructured_line_returns_raw():
    """DirectionalSpread / Collar traceback-lines: no ts, no format."""
    raw = "CS Scan: 1 found, 1 executed, 0 skipped | Mode: LIVE"
    p = parse_log_line(raw)
    assert p["ts"] is None
    assert p["level"] is None
    assert p["module"] is None
    assert p["message"] is None
    assert p["raw"] == raw


def test_parse_empty_line():
    p = parse_log_line("")
    assert p["raw"] == ""
    assert p["level"] is None


def test_parse_strips_trailing_newline():
    p = parse_log_line("plain text\n")
    assert p["raw"] == "plain text"


# ── Route integration tests ───────────────────────────────────────────────


@pytest.fixture
def client_with_logs(tmp_path, monkeypatch):
    """TestClient with a tmp-path fake home for the ic strategy's log."""
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [{"token": VALID_TOKEN, "label": "test"}],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    # Point the IC strategy's home to tmp_path/.nodeble/logs/
    fake_home = tmp_path / "home"
    (fake_home / ".nodeble" / "logs").mkdir(parents=True)
    # Route uses default Path.home(); easiest patch is on Path.home globally.
    monkeypatch.setattr(
        state_reader,
        "strategy_log_path",
        lambda sid, home=None: (
            fake_home / ".nodeble" / "logs" / "nodeble.log" if sid == "ic" else None
        ),
    )
    # Route imports strategy_log_path by name — patch the route module's
    # copy too.
    import nodeble_api_server.routes.strategies as routes_mod

    monkeypatch.setattr(
        routes_mod,
        "strategy_log_path",
        lambda sid, home=None: (
            fake_home / ".nodeble" / "logs" / "nodeble.log" if sid == "ic" else None
        ),
    )

    return TestClient(app), fake_home / ".nodeble" / "logs" / "nodeble.log"


def test_route_unknown_strategy_404(client_with_logs):
    client, _ = client_with_logs
    r = client.get(
        "/api/v1/strategies/bogus/logs",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 404


def test_route_missing_log_file_returns_empty_200(client_with_logs):
    """strategy id valid, file doesn't exist → empty lines, not 404."""
    client, _ = client_with_logs
    r = client.get(
        "/api/v1/strategies/ic/logs",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"lines": [], "cursor": 0, "truncated": False}


def test_route_reads_existing_log(client_with_logs):
    client, log_path = client_with_logs
    log_path.write_text(
        "\n".join(
            [
                "2026-04-20 14:00:00 nodeble INFO a",
                "2026-04-20 14:00:01 nodeble INFO b",
            ]
        )
        + "\n"
    )
    r = client.get(
        "/api/v1/strategies/ic/logs",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["lines"]) == 2
    assert body["lines"][1]["message"] == "b"
    assert body["cursor"] == log_path.stat().st_size


def test_route_requires_auth(client_with_logs):
    client, _ = client_with_logs
    r = client.get("/api/v1/strategies/ic/logs")
    assert r.status_code == 401

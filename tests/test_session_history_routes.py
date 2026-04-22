"""Tests for /api/v1/strategies/{id}/history/sessions and .../detail routes.

Fixture pattern mirrors test_logs.py — a tmp-home with a fake log file
monkeypatched onto the `ic` strategy's log path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, state_reader
from nodeble_api_server.app import app

VALID_TOKEN = "sess-hist-test-token"


@pytest.fixture
def client_with_log(tmp_path, monkeypatch):
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [{"token": VALID_TOKEN, "label": "t"}],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    log_file = tmp_path / "home" / ".nodeble" / "logs" / "nodeble.log"
    log_file.parent.mkdir(parents=True)

    def _fake_path(sid, home=None):
        return log_file if sid == "ic" else None

    monkeypatch.setattr(state_reader, "strategy_log_path", _fake_path)
    # The route module imports strategy_log_path by name, so patch there too.
    import nodeble_api_server.routes.strategies as routes_mod

    monkeypatch.setattr(routes_mod, "strategy_log_path", _fake_path)

    return TestClient(app), log_file


def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ── /history/sessions ─────────────────────────────────────────────────────


def test_sessions_requires_auth(client_with_log):
    client, _ = client_with_log
    r = client.get("/api/v1/strategies/ic/history/sessions")
    assert r.status_code == 401


def test_sessions_unknown_strategy_404(client_with_log):
    client, _ = client_with_log
    r = client.get("/api/v1/strategies/bogus/history/sessions", headers=_hdr())
    assert r.status_code == 404


def test_sessions_missing_log_file_returns_empty(client_with_log):
    client, _ = client_with_log
    r = client.get("/api/v1/strategies/ic/history/sessions", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"sessions": [], "has_more": False}


def test_sessions_unconfigured_strategy_returns_empty(client_with_log):
    """Route is hit for a strategy whose log_path maps to None."""
    client, _ = client_with_log
    # wheel isn't in our fake mapping (only ic is) → returns empty but 200.
    # First confirm wheel is in the registry so we don't hit 404.
    import nodeble_api_server.state_reader as sr
    if "wheel" not in sr.STRATEGY_REGISTRY:
        pytest.skip("wheel not in registry")
    r = client.get("/api/v1/strategies/wheel/history/sessions", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"sessions": [], "has_more": False}


def test_sessions_groups_cron_runs(client_with_log):
    client, log_file = client_with_log
    # Two "cron runs" 10 min apart. 300 s gap > 180 s threshold.
    _write_log(
        log_file,
        [
            "2026-04-22 14:00:00 nodeble INFO run 1 line a",
            "2026-04-22 14:00:02 nodeble INFO run 1 line b",
            "2026-04-22 14:00:03 nodeble WARNING run 1 line c",
            # 10-min gap
            "2026-04-22 14:10:00 nodeble INFO run 2 line a",
            "2026-04-22 14:10:01 nodeble INFO run 2 line b",
        ],
    )
    r = client.get("/api/v1/strategies/ic/history/sessions", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 2
    # Newest first.
    newest = body["sessions"][0]
    assert newest["line_count"] == 2
    assert newest["level_counts"] == {"INFO": 2}
    assert newest["start_ts"].startswith("2026-04-22T14:10:00")

    oldest = body["sessions"][1]
    assert oldest["line_count"] == 3
    assert oldest["level_counts"] == {"INFO": 2, "WARNING": 1}


def test_sessions_has_more_when_limit_reached(client_with_log):
    client, log_file = client_with_log
    # 5 sessions, each 5 min apart.
    lines = []
    for i in range(5):
        minute = i * 5  # 0, 5, 10, 15, 20 — each 300s apart
        lines.append(f"2026-04-22 15:{minute:02d}:00 nodeble INFO run-{i}")
    _write_log(log_file, lines)

    r = client.get(
        "/api/v1/strategies/ic/history/sessions?limit=3",
        headers=_hdr(),
    )
    body = r.json()
    assert len(body["sessions"]) == 3
    assert body["has_more"] is True


def test_sessions_before_ts_pagination(client_with_log):
    client, log_file = client_with_log
    lines = []
    for i in range(5):
        minute = i * 5
        lines.append(f"2026-04-22 16:{minute:02d}:00 nodeble INFO run-{i}")
    _write_log(log_file, lines)

    # First page: limit=2 → newest 2.
    r1 = client.get(
        "/api/v1/strategies/ic/history/sessions?limit=2",
        headers=_hdr(),
    )
    body1 = r1.json()
    assert len(body1["sessions"]) == 2
    assert body1["has_more"] is True
    oldest_on_page = body1["sessions"][-1]["start_ts"]

    # Second page: before_ts = oldest of page 1.
    r2 = client.get(
        f"/api/v1/strategies/ic/history/sessions?limit=2&before_ts={oldest_on_page}",
        headers=_hdr(),
    )
    body2 = r2.json()
    assert len(body2["sessions"]) == 2
    # Should be 2 older sessions.
    for s in body2["sessions"]:
        assert s["start_ts"] < oldest_on_page


def test_sessions_limit_clamped_to_100(client_with_log):
    client, log_file = client_with_log
    _write_log(log_file, ["2026-04-22 17:00:00 nodeble INFO only"])
    # Request absurd limit; should not 400.
    r = client.get(
        "/api/v1/strategies/ic/history/sessions?limit=99999",
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert len(r.json()["sessions"]) == 1


# ── /history/sessions/detail ──────────────────────────────────────────────


def test_detail_requires_auth(client_with_log):
    client, _ = client_with_log
    r = client.get(
        "/api/v1/strategies/ic/history/sessions/detail"
        "?start_ts=2026-04-22T14:30:00-04:00"
        "&end_ts=2026-04-22T14:30:10-04:00",
    )
    assert r.status_code == 401


def test_detail_unknown_strategy_404(client_with_log):
    client, _ = client_with_log
    r = client.get(
        "/api/v1/strategies/bogus/history/sessions/detail"
        "?start_ts=2026-04-22T14:30:00-04:00"
        "&end_ts=2026-04-22T14:30:10-04:00",
        headers=_hdr(),
    )
    assert r.status_code == 404


def test_detail_missing_params_400(client_with_log):
    """FastAPI automatic validation — missing query params → 422."""
    client, _ = client_with_log
    r = client.get(
        "/api/v1/strategies/ic/history/sessions/detail",
        headers=_hdr(),
    )
    # FastAPI emits 422 for missing required query args (closer to "invalid"
    # than "auth failure"); the exact code is a framework detail but shouldn't
    # be 200.
    assert r.status_code >= 400


def test_detail_returns_full_lines_in_window(client_with_log):
    client, log_file = client_with_log
    # One session of 5 lines + another earlier session of 2 lines.
    _write_log(
        log_file,
        [
            "2026-04-22 14:00:00 nodeble INFO before-1",
            "2026-04-22 14:00:01 nodeble INFO before-2",
            # Gap
            "2026-04-22 14:10:00 nodeble INFO target-start",
            "2026-04-22 14:10:02 nodeble INFO target-mid",
            "2026-04-22 14:10:04 nodeble ERROR target-err",
            "2026-04-22 14:10:05 nodeble INFO target-end",
        ],
    )
    # First, list sessions to grab the newest's window.
    listing = client.get(
        "/api/v1/strategies/ic/history/sessions",
        headers=_hdr(),
    ).json()
    target = listing["sessions"][0]

    r = client.get(
        "/api/v1/strategies/ic/history/sessions/detail"
        f"?start_ts={target['start_ts']}"
        f"&end_ts={target['end_ts']}",
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["lines"]) == 4
    messages = [ln["message"] for ln in body["lines"]]
    assert messages == ["target-start", "target-mid", "target-err", "target-end"]


def test_detail_includes_traceback_tail(client_with_log):
    """No-ts lines that follow an in-range ts-bearing line must come
    through — that's how Python tracebacks surface in our logs."""
    client, log_file = client_with_log
    _write_log(
        log_file,
        [
            "2026-04-22 14:10:00 nodeble ERROR crash happened",
            "Traceback (most recent call last):",
            '  File "foo.py", line 42, in bar',
            "    raise ValueError()",
            "ValueError",
        ],
    )
    # Listing should wrap all 5 lines into one session.
    listing = client.get(
        "/api/v1/strategies/ic/history/sessions",
        headers=_hdr(),
    ).json()
    assert len(listing["sessions"]) == 1
    target = listing["sessions"][0]
    assert target["line_count"] == 5

    r = client.get(
        "/api/v1/strategies/ic/history/sessions/detail"
        f"?start_ts={target['start_ts']}"
        f"&end_ts={target['end_ts']}",
        headers=_hdr(),
    )
    body = r.json()
    assert len(body["lines"]) == 5
    # Traceback lines come through as raw-only (ts None).
    assert body["lines"][0]["level"] == "ERROR"
    assert body["lines"][1]["ts"] is None
    assert "Traceback" in body["lines"][1]["raw"]


def test_detail_missing_log_file_returns_empty(client_with_log):
    client, _ = client_with_log
    r = client.get(
        "/api/v1/strategies/ic/history/sessions/detail"
        "?start_ts=2026-04-22T14:30:00-04:00"
        "&end_ts=2026-04-22T14:30:10-04:00",
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"lines": []}

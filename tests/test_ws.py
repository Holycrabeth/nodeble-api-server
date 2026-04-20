"""Tests for /api/v1/ws — auth, initial snapshot, multi-client broadcast,
disconnect cleanup, and broadcast_loop deadline semantics.

Uses starlette TestClient (sync WS) + its anyio portal to drive server-side
async work from sync test bodies.
"""
import asyncio
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nodeble_api_server import config, snapshot, ws
from nodeble_api_server.app import app

VALID_TOKEN = "ws-test-token-abc"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_path: Path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [
                        {"token": VALID_TOKEN, "label": "ws-test"},
                    ],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    # Don't let the lifespan loop tick during tests — we drive ticks by hand.
    monkeypatch.setattr(ws, "BROADCAST_INTERVAL_SEC", 3600.0)
    # Avoid hitting real ~/.nodeble-* on Tower.
    monkeypatch.setattr(snapshot, "list_installed_strategies", lambda: [])
    return TestClient(app)


def test_ws_requires_token(client):
    with client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws") as wsock:
            wsock.receive_text()


def test_ws_rejects_invalid_token(client):
    with client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws?token=wrong") as wsock:
            wsock.receive_text()


def test_ws_valid_token_gets_initial_snapshot(client):
    with client:
        with client.websocket_connect(f"/api/v1/ws?token={VALID_TOKEN}") as wsock:
            msg = wsock.receive_json()
            assert msg["type"] == "snapshot"
            assert "strategies" in msg["data"]
            assert "server_info" in msg["data"]
            assert msg["data"]["server_info"]["api_version"] == "v1"


def test_ws_multi_client_broadcast_reaches_all(client):
    with client:
        with client.websocket_connect(f"/api/v1/ws?token={VALID_TOKEN}") as w1:
            with client.websocket_connect(f"/api/v1/ws?token={VALID_TOKEN}") as w2:
                # Drain the per-client initial snapshots.
                w1.receive_json()
                w2.receive_json()
                # Trigger a broadcast inside the test client's async loop.
                client.portal.call(ws._broadcast_tick, ws.manager)
                m1 = w1.receive_json()
                m2 = w2.receive_json()
                assert m1 == m2
                assert m1["type"] == "snapshot"


def test_ws_revoked_token_rejected_on_next_handshake(client, tmp_path):
    """Contract: _authenticate reloads config on every handshake, so clearing
    valid_tokens in api.yaml takes effect without server restart. Guards
    against future regressions that try to cache load_config() results."""
    with client:
        with client.websocket_connect(f"/api/v1/ws?token={VALID_TOKEN}") as w1:
            w1.receive_json()  # first connection still valid

        # Rewrite the same api.yaml the fixture pointed DEFAULT_CONFIG_PATH at,
        # clearing tokens. No process restart, no config reload call.
        cfg_path = tmp_path / "api.yaml"
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "server": {"host": "127.0.0.1", "port": 8765},
                    "auth": {"valid_tokens": []},
                }
            )
        )

        # Next handshake with the same token must now fail.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/ws?token={VALID_TOKEN}") as w2:
                w2.receive_text()


def test_ws_disconnect_removes_client_from_manager(client):
    with client:
        before = client.portal.call(ws.manager.client_count)
        with client.websocket_connect(f"/api/v1/ws?token={VALID_TOKEN}") as wsock:
            wsock.receive_json()  # initial snapshot
            during = client.portal.call(ws.manager.client_count)
            assert during == before + 1
        # Context exit sends WebSocketDisconnect; handler removes client.
        # Give the server loop one cycle to process the disconnect.
        for _ in range(20):
            after = client.portal.call(ws.manager.client_count)
            if after == before:
                break
            time.sleep(0.01)
        assert after == before


# ── ConnectionManager unit tests ───────────────────────────────────────────


class _FakeWS:
    """Minimal async stand-in for a starlette WebSocket."""

    def __init__(self, fail_on_send: bool = False):
        self.fail = fail_on_send
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.sent.append(payload)


def test_connection_manager_broadcast_drops_dead_peers():
    async def go():
        mgr = ws.ConnectionManager()
        ok = _FakeWS()
        bad = _FakeWS(fail_on_send=True)
        await mgr.connect(ok)  # type: ignore[arg-type]
        await mgr.connect(bad)  # type: ignore[arg-type]
        await mgr.broadcast('{"type": "t", "data": {}}')
        # Healthy peer received; dead peer got evicted.
        assert ok.sent == ['{"type": "t", "data": {}}']
        assert await mgr.client_count() == 1

    asyncio.run(go())


# ── broadcast_loop deadline semantics ─────────────────────────────────────


def test_broadcast_loop_paces_fast_ticks_at_interval(monkeypatch):
    """Fast ticks (work << interval) must be paced by the deadline so we
    don't burn CPU. 3+ ticks at 0.05s interval → call gaps close to 0.05s.
    """
    calls: list[float] = []

    async def fast_tick(_mgr):
        calls.append(time.monotonic())

    monkeypatch.setattr(ws, "_broadcast_tick", fast_tick)

    async def drive():
        task = asyncio.create_task(
            ws.broadcast_loop(ws.ConnectionManager(), interval=0.05)
        )
        await asyncio.sleep(0.22)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert len(calls) >= 3, f"expected ≥3 ticks, got {len(calls)}"
    for prev, curr in zip(calls, calls[1:]):
        gap = curr - prev
        # Gap must be ≥ interval (paced) — small upper bound keeps flakiness
        # low while still catching regressions that remove the sleep entirely.
        assert 0.04 <= gap <= 0.12, f"gap {gap:.3f}s out of bounds"


def test_broadcast_loop_survives_broken_tick(monkeypatch):
    """If a tick raises, the loop logs and keeps going — a single bad
    read_state call must not take the whole broadcaster down.
    """
    call_count = {"n": 0}

    async def flaky_tick(_mgr):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("synthetic failure")

    monkeypatch.setattr(ws, "_broadcast_tick", flaky_tick)

    async def drive():
        task = asyncio.create_task(
            ws.broadcast_loop(ws.ConnectionManager(), interval=0.02)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert call_count["n"] >= 4, "loop should keep ticking after one exception"

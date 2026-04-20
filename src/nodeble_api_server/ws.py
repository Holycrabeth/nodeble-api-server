"""WebSocket endpoint + broadcaster for live state push.

Single endpoint `/api/v1/ws` with token auth via `?token=...` query param
(WS browsers can't set custom headers pre-handshake). On accept, the server
immediately pushes the current snapshot so clients don't sit on stale HTTP
data waiting for the next tick. A background task then broadcasts the full
state_reader output every `BROADCAST_INTERVAL_SEC`. No diff — each snapshot
is <10KB and client `setQueryData` naturally overwrites.

Access to the endpoint bypasses the api_v1 Bearer dependency because the
FastAPI Depends tree doesn't reach WebSocket handshakes the same way; we
authenticate inline against `config.tokens` with `secrets.compare_digest`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets

from fastapi import APIRouter, Query, WebSocket, status
from starlette.websockets import WebSocketDisconnect

from nodeble_api_server.config import load_config
from nodeble_api_server.snapshot import build_snapshot

log = logging.getLogger(__name__)

BROADCAST_INTERVAL_SEC = 5.0

router = APIRouter()


class ConnectionManager:
    """Tracks active WS clients; broadcasts JSON payloads with dead-peer cleanup."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def client_count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def broadcast(self, payload: str) -> None:
        """Fan out one already-encoded JSON frame to every peer. Takes a
        pre-encoded string so we only pay json.dumps once per tick regardless
        of client count, and so every client sees byte-identical data.
        """
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


# Module-level singleton — lifespan task + endpoint share this instance.
manager = ConnectionManager()


def _authenticate(token: str) -> bool:
    """Timing-safe compare against configured tokens. Re-loads config on each
    call so revocation takes effect without server restart — same contract as
    the HTTP Bearer check in auth.require_bearer_token."""
    if not token:
        return False
    cfg = load_config()
    for entry in cfg.tokens:
        if secrets.compare_digest(entry.token, token):
            return True
    return False


def _encode_snapshot() -> str:
    """Build the snapshot envelope and encode once. Single source of truth
    for wire format — envelope fields (ts, seq, …) only get added here.
    """
    return json.dumps({"type": "snapshot", "data": build_snapshot()})


async def _broadcast_tick(conn_manager: ConnectionManager) -> None:
    """Build + broadcast one snapshot. Isolated for direct unit testing."""
    await conn_manager.broadcast(_encode_snapshot())


async def broadcast_loop(
    conn_manager: ConnectionManager,
    interval: float | None = None,
) -> None:
    """Deadline-based tick loop. If a tick takes longer than `interval`,
    the next one runs immediately with zero sleep — but never 'catches up'
    by running multiple ticks back-to-back. Prevents snowball when
    state_reader is occasionally slow.

    `interval` defaults to the module-level BROADCAST_INTERVAL_SEC read at
    call time, so tests can monkeypatch the constant without relying on
    argument injection into the lifespan task.
    """
    if interval is None:
        interval = BROADCAST_INTERVAL_SEC
    loop = asyncio.get_running_loop()
    while True:
        deadline = loop.time() + interval
        try:
            await _broadcast_tick(conn_manager)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("broadcast_tick failed")
        sleep_for = max(0.0, deadline - loop.time())
        await asyncio.sleep(sleep_for)


@router.websocket("/api/v1/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")) -> None:
    if not _authenticate(token):
        # Close before accept → client sees 403 / policy violation at handshake.
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    await manager.connect(ws)
    try:
        # Immediate snapshot — don't make the client wait for the next tick.
        await ws.send_text(_encode_snapshot())
        # Consume any client-sent frames so disconnects propagate via
        # WebSocketDisconnect. Clients currently don't send anything; this
        # loop is the heartbeat listener.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws handler error")
    finally:
        await manager.disconnect(ws)

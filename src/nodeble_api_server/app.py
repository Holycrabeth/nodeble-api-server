"""FastAPI application for nodeble-api-server.

/health is intentionally public. All other HTTP routes go under /api/v1
and depend on `auth.require_bearer_token`. The WS endpoint /api/v1/ws
authenticates via query-param token (handled inline in ws.py).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI

from nodeble_api_server import __version__, ws
from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.snapshot import build_server_info
from nodeble_api_server.snapshot_writer import (
    SERVER_TZ as _SNAPSHOT_TZ,
    _next_snapshot_time,
    take_daily_snapshot,
)

log = logging.getLogger(__name__)


async def _snapshot_loop() -> None:
    """Seed-on-boot + one snapshot per ET 23:59 day.

    Startup: fire take_daily_snapshot() immediately so today's first
    point appears in the chart without waiting for midnight. Writer
    is idempotent per (strategy, date), so restarting the process
    mid-day won't duplicate rows.

    Steady state: sleep until the next 23:59 ET, snapshot, loop.
    Exceptions are logged and swallowed — a bad day shouldn't take
    the scheduler down."""
    try:
        written = take_daily_snapshot()
        if written:
            log.info("snapshot_loop: seeded %d row(s)", len(written))
    except Exception:
        log.exception("snapshot_loop: initial seed failed")

    while True:
        try:
            now = datetime.now(_SNAPSHOT_TZ)
            next_run = _next_snapshot_time(now)
            delay = (next_run - now).total_seconds()
            await asyncio.sleep(max(1.0, delay))
            written = take_daily_snapshot()
            if written:
                log.info("snapshot_loop: wrote %d row(s)", len(written))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("snapshot_loop: tick failed")
            # On any transient failure, wait 60s then try the schedule
            # again — prevents a tight error loop if the file system
            # hiccups.
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ws_task = asyncio.create_task(ws.broadcast_loop(ws.manager))
    snapshot_task = asyncio.create_task(_snapshot_loop())
    try:
        yield
    finally:
        for task in (ws_task, snapshot_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="NODEBLE API Server", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_bearer_token)])


@api_v1.get("/server/info")
def server_info() -> dict:
    return build_server_info()


app.include_router(api_v1)

# Strategy-scoped routes live in their own module for clarity.
from nodeble_api_server.routes import strategies as _strategies  # noqa: E402
from nodeble_api_server.routes import market as _market  # noqa: E402
from nodeble_api_server.routes import orchestrator as _orchestrator  # noqa: E402
from nodeble_api_server.routes import system as _system  # noqa: E402
from nodeble_api_server.routes import server as _server  # noqa: E402
from nodeble_api_server.routes import daily_summary as _daily_summary  # noqa: E402

app.include_router(_strategies.router)
app.include_router(_market.router)
app.include_router(_orchestrator.router)
app.include_router(_system.router)
app.include_router(_server.router)  # /api/v1/server/* — GUI v1 install wizard backend
app.include_router(_daily_summary.router)  # /api/v1/server/daily-summary — Dashboard "今日运营"
app.include_router(ws.router)


# Phase A Week 2: on api-server boot, mark any in-flight installs as failed.
# Subprocesses don't survive systemd restart, so any 'running'/'queued' state
# is stale and would confuse SSE/status endpoints.
@app.on_event("startup")
def _cleanup_stale_installs() -> None:
    from nodeble_api_server import install_state as _install_state
    cleaned = _install_state.cleanup_stale_running()
    if cleaned:
        import logging
        logging.getLogger(__name__).info(
            "Phase A Week 2 boot cleanup: marked %d stale installs as failed: %s",
            len(cleaned), cleaned,
        )

"""FastAPI application for nodeble-api-server.

/health is intentionally public. All other HTTP routes go under /api/v1
and depend on `auth.require_bearer_token`. The WS endpoint /api/v1/ws
authenticates via query-param token (handled inline in ws.py).
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI

from nodeble_api_server import __version__, ws
from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.snapshot import build_server_info


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(ws.broadcast_loop(ws.manager))
    try:
        yield
    finally:
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

app.include_router(_strategies.router)
app.include_router(_market.router)
app.include_router(ws.router)

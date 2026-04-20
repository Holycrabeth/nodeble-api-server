"""FastAPI application for nodeble-api-server.

/health is intentionally public. All other routes go under /api/v1 and
depend on `auth.require_bearer_token`.
"""
import socket
import time

from fastapi import APIRouter, Depends, FastAPI

from nodeble_api_server import __version__
from nodeble_api_server.auth import require_bearer_token

API_VERSION = "v1"
_START_TIME = time.monotonic()

app = FastAPI(title="NODEBLE API Server", version=__version__)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_bearer_token)])


@api_v1.get("/server/info")
def server_info() -> dict:
    return {
        "version": __version__,
        "api_version": API_VERSION,
        "hostname": socket.gethostname(),
        "uptime_sec": int(time.monotonic() - _START_TIME),
    }


app.include_router(api_v1)

# Strategy-scoped routes live in their own module for clarity.
from nodeble_api_server.routes import strategies as _strategies  # noqa: E402

app.include_router(_strategies.router)

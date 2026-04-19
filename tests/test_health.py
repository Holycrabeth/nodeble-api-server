from fastapi.testclient import TestClient

from nodeble_api_server import __version__
from nodeble_api_server.app import app

client = TestClient(app)


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}

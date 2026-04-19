"""Tests for /api/v1/server/info."""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import __version__, config
from nodeble_api_server.app import app

VALID_TOKEN = "server-info-token-xyz"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_path: Path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [
                        {"token": VALID_TOKEN, "label": "test"},
                    ],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    return TestClient(app)


def test_server_info_requires_auth(client):
    r = client.get("/api/v1/server/info")
    assert r.status_code == 401


def test_server_info_returns_fields(client):
    r = client.get(
        "/api/v1/server/info",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == __version__
    assert data["api_version"] == "v1"
    assert isinstance(data["hostname"], str) and len(data["hostname"]) > 0
    assert isinstance(data["uptime_sec"], int) and data["uptime_sec"] >= 0


def test_server_info_uptime_monotonic(client):
    import time

    r1 = client.get(
        "/api/v1/server/info",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    ).json()
    time.sleep(0.05)
    r2 = client.get(
        "/api/v1/server/info",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    ).json()
    assert r2["uptime_sec"] >= r1["uptime_sec"]

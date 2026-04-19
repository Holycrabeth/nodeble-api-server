"""Auth middleware tests — verify /health stays public and protected routes enforce Bearer token."""
from pathlib import Path

import pytest
import yaml
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nodeble_api_server import config
from nodeble_api_server.auth import require_bearer_token

VALID_TOKEN = "test-token-abc123"


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "api.yaml"
    cfg.write_text(
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
    return cfg


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh FastAPI app with a protected test route + isolated config."""
    cfg_path = _write_config(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    app = FastAPI()

    @app.get("/protected")
    def protected(token: str = Depends(require_bearer_token)):
        return {"token_suffix": token[-6:]}

    return TestClient(app)


def test_no_auth_header_returns_401(client):
    r = client.get("/protected")
    assert r.status_code == 401


def test_wrong_token_returns_401(client):
    r = client.get("/protected", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_wrong_scheme_returns_401(client):
    r = client.get("/protected", headers={"Authorization": f"Basic {VALID_TOKEN}"})
    assert r.status_code == 401


def test_valid_token_returns_200(client):
    r = client.get("/protected", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    assert r.json()["token_suffix"] == VALID_TOKEN[-6:]


def test_health_is_public(tmp_path, monkeypatch):
    """/health must never require a token (independent of auth config)."""
    cfg_path = _write_config(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    from nodeble_api_server.app import app
    from nodeble_api_server import __version__

    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}

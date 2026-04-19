"""Config loader for nodeble-api-server.

Reads ~/.nodeble-api/config/api.yaml. Covers server, auth.valid_tokens, and
tls.{cert_path,key_path,fingerprint}. Unknown fields are preserved silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.nodeble-api/config/api.yaml").expanduser()


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


@dataclass(frozen=True)
class TokenEntry:
    token: str
    label: str


@dataclass(frozen=True)
class TlsConfig:
    cert_path: Path | None
    key_path: Path | None
    fingerprint: str | None

    @property
    def enabled(self) -> bool:
        return (
            self.cert_path is not None
            and self.key_path is not None
            and self.cert_path.exists()
            and self.key_path.exists()
        )


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    tokens: tuple[TokenEntry, ...]
    tls: TlsConfig


def load_config(path: Path | None = None) -> AppConfig:
    """Load full config from api.yaml.

    Falls back to safe defaults (0.0.0.0:8765, no tokens, no TLS) when the
    file does not exist — useful for dev before deploy.sh writes the template.

    When `path` is None, re-resolves DEFAULT_CONFIG_PATH at call time (so
    tests can monkeypatch it without import-order tricks).
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH
    if not path.exists():
        return AppConfig(
            server=ServerConfig(host="0.0.0.0", port=8765),
            tokens=(),
            tls=TlsConfig(cert_path=None, key_path=None, fingerprint=None),
        )

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    server_raw = data.get("server") or {}
    server = ServerConfig(
        host=str(server_raw.get("host", "0.0.0.0")),
        port=int(server_raw.get("port", 8765)),
    )

    auth_raw = data.get("auth") or {}
    tokens_raw = auth_raw.get("valid_tokens") or []
    tokens = tuple(
        TokenEntry(token=str(entry["token"]), label=str(entry["label"]))
        for entry in tokens_raw
        if isinstance(entry, dict) and "token" in entry and "label" in entry
    )

    tls_raw = data.get("tls") or {}
    tls = TlsConfig(
        cert_path=Path(tls_raw["cert_path"]) if tls_raw.get("cert_path") else None,
        key_path=Path(tls_raw["key_path"]) if tls_raw.get("key_path") else None,
        fingerprint=str(tls_raw["fingerprint"]) if tls_raw.get("fingerprint") else None,
    )

    return AppConfig(server=server, tokens=tokens, tls=tls)


def load_server_config(path: Path | None = None) -> ServerConfig:
    """Back-compat helper — prefer load_config() for new code."""
    return load_config(path).server

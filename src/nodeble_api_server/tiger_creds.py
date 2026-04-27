"""Tiger broker credentials disk storage.

Phase A Week 2 per Phase 4.1 contract freeze §1.3 + Backend Director plan
Task A.3.

Stores Tiger API creds (id + account + private_key_pem) to disk for re-use
across all 9 strategy module installs. Path: ~/.nodeble-api/secrets/tiger.yaml
with file mode 0600.

Security
--------
- File written with mode 0600 (owner read/write only)
- Parent dir mode 0700
- private_key_pem NEVER returned via GET endpoint (only `account` field)
- Does NOT log the private key under any circumstance
- atomic write via tempfile + os.replace to prevent torn writes

Usage
-----
- PUT /api/v1/server/credentials/tiger calls store(payload)
- GET /api/v1/server/credentials/tiger calls summary() — returns
  {exists, account, stored_at} without secret
- Strategy installs call read_for_install() to get full creds for
  passing to deploy.sh (NEVER logged or returned via API)
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_paths(home: Path | None = None) -> tuple[Path, Path]:
    """Return (secrets_dir, tiger_yaml_path). Path.home() resolved lazily so
    test monkeypatch + runtime environment changes both work correctly.
    """
    base = home if home is not None else Path.home()
    secrets_dir = base / ".nodeble-api" / "secrets"
    return secrets_dir, secrets_dir / "tiger.yaml"


def _atomic_write(path: Path, content: str, file_mode: int = _FILE_MODE) -> None:
    """Atomic write — tempfile in same dir then os.replace, set mode 0600."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, _DIR_MODE)
    except OSError:
        pass  # mode chmod may fail on some FS; not fatal

    fd, tmp_path = tempfile.mkstemp(dir=str(parent), prefix=".tiger_", suffix=".yaml.tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.chmod(tmp_path, file_mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def store(
    *,
    tiger_id: str,
    tiger_account: str,
    private_key_pem: str,
    home: Path | None = None,
) -> dict[str, Any]:
    """Persist Tiger creds to disk. Returns summary dict (no secret)."""
    if not tiger_id or not tiger_account or not private_key_pem:
        raise ValueError("tiger_id, tiger_account, private_key_pem all required non-empty")

    _, path = _resolve_paths(home)
    stored_at = _utc_iso()
    payload = {
        "tiger_id": tiger_id,
        "tiger_account": tiger_account,
        "private_key_pem": private_key_pem,
        "stored_at": stored_at,
    }
    content = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    _atomic_write(path, content)

    return {
        "status": "stored",
        "account": tiger_account,
        "stored_at": stored_at,
    }


def summary(home: Path | None = None) -> dict[str, Any]:
    """Returns presence info WITHOUT secret. Used by GET endpoint.

    Schema: {exists: bool, account: str | None, stored_at: str | None}
    """
    _, path = _resolve_paths(home)
    if not path.exists():
        return {"exists": False, "account": None, "stored_at": None}

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {"exists": False, "account": None, "stored_at": None}

    return {
        "exists": True,
        "account": data.get("tiger_account"),
        "stored_at": data.get("stored_at"),
    }


def read_for_install(home: Path | None = None) -> dict[str, str] | None:
    """Returns FULL creds dict for passing to deploy.sh subprocess.

    Returns None if creds not present (caller should error 422).
    Result MUST never be logged or returned via API to client.
    """
    _, path = _resolve_paths(home)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not all(k in data for k in ("tiger_id", "tiger_account", "private_key_pem")):
        return None
    return {
        "tiger_id": data["tiger_id"],
        "tiger_account": data["tiger_account"],
        "private_key_pem": data["private_key_pem"],
    }


def delete(home: Path | None = None) -> bool:
    """Remove creds file. Returns True if file existed and was deleted."""
    _, path = _resolve_paths(home)
    if path.exists():
        path.unlink()
        return True
    return False

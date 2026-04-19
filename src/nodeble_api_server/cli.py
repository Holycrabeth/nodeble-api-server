"""Admin CLI subcommands: generate-token, revoke-token, generate-cert.

Wired up via __main__.py argparse. Tokens live in api.yaml; certs live under
~/.nodeble-api/certs/. YAML writes are atomic (tempfile + os.replace) to
avoid half-written config on crash.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

from nodeble_api_server.config import DEFAULT_CONFIG_PATH

DEFAULT_CERTS_DIR = Path("~/.nodeble-api/certs").expanduser()
CERT_VALIDITY_DAYS = 3650  # 10 years — self-signed, pinned by fingerprint


def _atomic_write_yaml(path: Path, data: dict) -> None:
    """Write YAML atomically (tempfile + os.replace). Mirrors bot_helpers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def _load_yaml_or_empty(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def generate_token(label: str, cfg_path: Path = DEFAULT_CONFIG_PATH) -> str:
    """Generate a UUID4 token, append to api.yaml valid_tokens. Returns token."""
    if not label or not label.strip():
        print("ERROR: label must be a non-empty string", file=sys.stderr)
        sys.exit(1)

    data = _load_yaml_or_empty(cfg_path)
    auth = data.setdefault("auth", {}) or {}
    tokens = auth.get("valid_tokens") or []

    for entry in tokens:
        if isinstance(entry, dict) and entry.get("label") == label:
            print(
                f"ERROR: token with label '{label}' already exists. "
                f"Revoke it first via: python -m nodeble_api_server revoke-token {label}",
                file=sys.stderr,
            )
            sys.exit(1)

    new_token = str(uuid.uuid4())
    tokens.append({"token": new_token, "label": label})
    auth["valid_tokens"] = tokens
    data["auth"] = auth

    _atomic_write_yaml(cfg_path, data)

    print(f"Token generated (label='{label}'):")
    print(f"  {new_token}")
    print("Copy this value into the NODEBLE desktop app during first-run setup.")
    return new_token


def revoke_token(label: str, cfg_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Remove the token entry matching `label`. Exits 1 if not found."""
    data = _load_yaml_or_empty(cfg_path)
    auth = data.get("auth") or {}
    tokens = auth.get("valid_tokens") or []

    remaining = [
        entry for entry in tokens
        if not (isinstance(entry, dict) and entry.get("label") == label)
    ]

    if len(remaining) == len(tokens):
        print(f"ERROR: no token with label '{label}' found.", file=sys.stderr)
        sys.exit(1)

    data.setdefault("auth", {})["valid_tokens"] = remaining
    _atomic_write_yaml(cfg_path, data)

    print(f"Token '{label}' revoked. {len(remaining)} token(s) remaining.")


def generate_cert(
    certs_dir: Path = DEFAULT_CERTS_DIR,
    cfg_path: Path = DEFAULT_CONFIG_PATH,
) -> tuple[Path, Path, str]:
    """Create self-signed cert via openssl. Returns (cert_path, key_path, fingerprint)."""
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert_path = certs_dir / "cert.pem"
    key_path = certs_dir / "key.pem"
    fingerprint_path = certs_dir / "fingerprint.txt"

    if cert_path.exists() and key_path.exists():
        existing = fingerprint_path.read_text().strip() if fingerprint_path.exists() else "(fingerprint.txt missing)"
        print(f"Cert already exists at {cert_path} — skipping.")
        print(f"Fingerprint: {existing}")
        return cert_path, key_path, existing

    subprocess.run(
        [
            "openssl", "req", "-x509",
            "-newkey", "rsa:4096",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", str(CERT_VALIDITY_DAYS),
            "-nodes",
            "-subj", "/CN=nodeble-api-server",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:0.0.0.0",
        ],
        check=True,
        capture_output=True,
    )
    key_path.chmod(0o600)

    result = subprocess.run(
        ["openssl", "x509", "-in", str(cert_path), "-fingerprint", "-sha256", "-noout"],
        check=True,
        capture_output=True,
        text=True,
    )
    # Output format: "sha256 Fingerprint=XX:XX:..."
    fingerprint = result.stdout.strip().split("=", 1)[1]

    fingerprint_path.write_text(fingerprint + "\n")

    data = _load_yaml_or_empty(cfg_path)
    tls = data.setdefault("tls", {}) or {}
    tls["cert_path"] = str(cert_path)
    tls["key_path"] = str(key_path)
    tls["fingerprint"] = fingerprint
    data["tls"] = tls
    _atomic_write_yaml(cfg_path, data)

    print(f"Certificate generated at {cert_path}")
    print(f"Key          at {key_path} (mode 600)")
    print(f"Fingerprint  {fingerprint}")
    print(f"Valid for    {CERT_VALIDITY_DAYS} days")
    return cert_path, key_path, fingerprint

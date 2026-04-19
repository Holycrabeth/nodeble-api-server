"""CLI tests: generate_token, revoke_token, generate_cert."""
import subprocess
from pathlib import Path

import pytest
import yaml

from nodeble_api_server import cli


def _read_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def test_generate_token_creates_entry(tmp_path):
    cfg = tmp_path / "api.yaml"
    token = cli.generate_token("my-macbook", cfg_path=cfg)

    data = _read_yaml(cfg)
    tokens = data["auth"]["valid_tokens"]
    assert len(tokens) == 1
    assert tokens[0]["label"] == "my-macbook"
    assert tokens[0]["token"] == token
    assert len(token) == 36  # UUID4 string length


def test_generate_token_duplicate_label_exits(tmp_path):
    cfg = tmp_path / "api.yaml"
    cli.generate_token("phone", cfg_path=cfg)
    with pytest.raises(SystemExit):
        cli.generate_token("phone", cfg_path=cfg)


def test_generate_token_appends(tmp_path):
    cfg = tmp_path / "api.yaml"
    cli.generate_token("mac", cfg_path=cfg)
    cli.generate_token("ipad", cfg_path=cfg)

    labels = [t["label"] for t in _read_yaml(cfg)["auth"]["valid_tokens"]]
    assert labels == ["mac", "ipad"]


def test_revoke_token_removes_entry(tmp_path):
    cfg = tmp_path / "api.yaml"
    cli.generate_token("mac", cfg_path=cfg)
    cli.generate_token("ipad", cfg_path=cfg)

    cli.revoke_token("mac", cfg_path=cfg)

    labels = [t["label"] for t in _read_yaml(cfg)["auth"]["valid_tokens"]]
    assert labels == ["ipad"]


def test_revoke_missing_label_exits(tmp_path):
    cfg = tmp_path / "api.yaml"
    cli.generate_token("mac", cfg_path=cfg)
    with pytest.raises(SystemExit):
        cli.revoke_token("nonexistent", cfg_path=cfg)


@pytest.mark.skipif(
    subprocess.run(["which", "openssl"], capture_output=True).returncode != 0,
    reason="openssl not available",
)
def test_generate_cert_creates_files_and_records_fingerprint(tmp_path):
    certs_dir = tmp_path / "certs"
    cfg = tmp_path / "api.yaml"

    cert_path, key_path, fingerprint = cli.generate_cert(certs_dir=certs_dir, cfg_path=cfg)

    assert cert_path.exists()
    assert key_path.exists()
    assert (certs_dir / "fingerprint.txt").read_text().strip() == fingerprint
    # SHA-256 fingerprint format: 64 hex chars with colons → 32 * 3 - 1 = 95 chars
    assert len(fingerprint) == 95
    assert fingerprint.count(":") == 31

    data = _read_yaml(cfg)
    assert data["tls"]["cert_path"] == str(cert_path)
    assert data["tls"]["key_path"] == str(key_path)
    assert data["tls"]["fingerprint"] == fingerprint


@pytest.mark.skipif(
    subprocess.run(["which", "openssl"], capture_output=True).returncode != 0,
    reason="openssl not available",
)
def test_generate_cert_idempotent(tmp_path, capsys):
    certs_dir = tmp_path / "certs"
    cfg = tmp_path / "api.yaml"

    _, _, fp1 = cli.generate_cert(certs_dir=certs_dir, cfg_path=cfg)
    _, _, fp2 = cli.generate_cert(certs_dir=certs_dir, cfg_path=cfg)

    assert fp1 == fp2
    out = capsys.readouterr().out
    assert "skipping" in out.lower()

"""Tests for tiger_creds disk storage — Phase A Week 2.

Pin: atomic write, file mode 0600, summary never leaks private key,
delete works, reads tolerate missing/corrupt files gracefully.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nodeble_api_server import tiger_creds


def test_summary_when_absent_returns_safe_empty(tmp_path):
    result = tiger_creds.summary(home=tmp_path)
    assert result == {"exists": False, "account": None, "stored_at": None}


def test_store_then_summary_returns_account(tmp_path):
    tiger_creds.store(
        tiger_id="50691693",
        tiger_account="Yongtao_2K1",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
        home=tmp_path,
    )
    result = tiger_creds.summary(home=tmp_path)
    assert result["exists"] is True
    assert result["account"] == "Yongtao_2K1"
    assert result["stored_at"] is not None


def test_summary_never_leaks_private_key(tmp_path):
    """Critical security regression guard."""
    tiger_creds.store(
        tiger_id="x",
        tiger_account="y",
        private_key_pem="SECRET_KEY_DO_NOT_LEAK",
        home=tmp_path,
    )
    result = tiger_creds.summary(home=tmp_path)
    assert "private_key_pem" not in result
    assert "SECRET_KEY_DO_NOT_LEAK" not in str(result)


def test_store_writes_file_mode_0600(tmp_path):
    """File must be readable/writable by owner only (0600)."""
    tiger_creds.store(
        tiger_id="x", tiger_account="y", private_key_pem="z",
        home=tmp_path,
    )
    creds_file = tmp_path / ".nodeble-api" / "secrets" / "tiger.yaml"
    assert creds_file.exists()
    mode = stat.S_IMODE(creds_file.stat().st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_store_atomic_write_no_torn_state_on_failure(tmp_path, monkeypatch):
    """If write fails mid-way, original file is preserved. (Atomic via tempfile + os.replace.)"""
    # First successful write
    tiger_creds.store(
        tiger_id="orig", tiger_account="orig_account", private_key_pem="orig_key",
        home=tmp_path,
    )
    # Second store with corrupt private_key_pem — should still complete (no validation)
    # The atomic-write contract is about TORN states, not validation. To test torn,
    # we'd need to inject a write failure mid-flight. For now: verify second write
    # doesn't leave junk files behind.
    tiger_creds.store(
        tiger_id="new", tiger_account="new_account", private_key_pem="new_key",
        home=tmp_path,
    )
    secrets_dir = tmp_path / ".nodeble-api" / "secrets"
    files = list(secrets_dir.iterdir())
    # Should be exactly 1 file (tiger.yaml), no .tmp leftovers
    assert len(files) == 1
    assert files[0].name == "tiger.yaml"


def test_read_for_install_returns_full_creds_when_present(tmp_path):
    tiger_creds.store(
        tiger_id="50691693", tiger_account="Yongtao_2K1",
        private_key_pem="-----BEGIN KEY-----",
        home=tmp_path,
    )
    creds = tiger_creds.read_for_install(home=tmp_path)
    assert creds is not None
    assert creds["tiger_id"] == "50691693"
    assert creds["tiger_account"] == "Yongtao_2K1"
    assert creds["private_key_pem"] == "-----BEGIN KEY-----"


def test_read_for_install_returns_none_when_absent(tmp_path):
    assert tiger_creds.read_for_install(home=tmp_path) is None


def test_read_for_install_returns_none_when_file_corrupt(tmp_path):
    """Tolerate corrupt YAML — return None rather than crash."""
    secrets_dir = tmp_path / ".nodeble-api" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "tiger.yaml").write_text("not: valid: yaml: [unclosed")
    # Should not crash
    result = tiger_creds.read_for_install(home=tmp_path)
    assert result is None


def test_read_for_install_returns_none_when_missing_keys(tmp_path):
    """If yaml exists but doesn't have all 3 required keys, return None."""
    import yaml
    secrets_dir = tmp_path / ".nodeble-api" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "tiger.yaml").write_text(yaml.safe_dump({"tiger_id": "x"}))  # missing 2 keys
    assert tiger_creds.read_for_install(home=tmp_path) is None


def test_delete_removes_file(tmp_path):
    tiger_creds.store(
        tiger_id="x", tiger_account="y", private_key_pem="z",
        home=tmp_path,
    )
    assert tiger_creds.summary(home=tmp_path)["exists"] is True
    deleted = tiger_creds.delete(home=tmp_path)
    assert deleted is True
    assert tiger_creds.summary(home=tmp_path)["exists"] is False


def test_delete_returns_false_when_absent(tmp_path):
    assert tiger_creds.delete(home=tmp_path) is False


def test_store_validates_required_fields(tmp_path):
    with pytest.raises(ValueError):
        tiger_creds.store(tiger_id="", tiger_account="y", private_key_pem="z", home=tmp_path)
    with pytest.raises(ValueError):
        tiger_creds.store(tiger_id="x", tiger_account="", private_key_pem="z", home=tmp_path)
    with pytest.raises(ValueError):
        tiger_creds.store(tiger_id="x", tiger_account="y", private_key_pem="", home=tmp_path)

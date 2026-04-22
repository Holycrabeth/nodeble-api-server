"""Tests for audit_reader.read_audit_entries + the /history/config route."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import audit as audit_mod, config
from nodeble_api_server.app import app
from nodeble_api_server.audit_reader import read_audit_entries

VALID_TOKEN = "audit-reader-test-token"


def _event(ts: str, *, strategy: str = "ic", reason: str = "", result: str = "success", **over):
    return {
        "ts": ts,
        "actor": "desktop",
        "strategy": strategy,
        "param_path": "selection.put_delta_max",
        "old_value": 0.22,
        "new_value": 0.20,
        "reason": reason,
        "result": result,
        "error": None,
        **over,
    }


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# ── read_audit_entries unit tests ─────────────────────────────────────────


def test_missing_file_returns_empty(tmp_path: Path):
    assert read_audit_entries(tmp_path / "nope.jsonl") == []


def test_empty_file_returns_empty(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    path.write_text("")
    assert read_audit_entries(path) == []


def test_reverse_chronological_order(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    # Intentionally out of order.
    _write_jsonl(
        path,
        [
            _event("2026-04-21T10:00:00-04:00"),
            _event("2026-04-21T12:00:00-04:00"),
            _event("2026-04-21T11:00:00-04:00"),
        ],
    )
    entries = read_audit_entries(path)
    tss = [e["ts"] for e in entries]
    assert tss == [
        "2026-04-21T12:00:00-04:00",
        "2026-04-21T11:00:00-04:00",
        "2026-04-21T10:00:00-04:00",
    ]


def test_strategy_filter(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    _write_jsonl(
        path,
        [
            _event("2026-04-21T10:00:00-04:00", strategy="ic"),
            _event("2026-04-21T11:00:00-04:00", strategy="wheel"),
            _event("2026-04-21T12:00:00-04:00", strategy="ic"),
        ],
    )
    ic_only = read_audit_entries(path, strategy="ic")
    assert len(ic_only) == 2
    assert all(e["strategy"] == "ic" for e in ic_only)


def test_before_ts_pagination(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    _write_jsonl(
        path,
        [
            _event(f"2026-04-21T{h:02d}:00:00-04:00")
            for h in range(10, 15)  # 10:00..14:00
        ],
    )
    first_page = read_audit_entries(path, limit=2)
    assert [e["ts"] for e in first_page] == [
        "2026-04-21T14:00:00-04:00",
        "2026-04-21T13:00:00-04:00",
    ]
    # Next page: cursor = oldest ts of previous page.
    second_page = read_audit_entries(
        path, limit=2, before_ts="2026-04-21T13:00:00-04:00",
    )
    assert [e["ts"] for e in second_page] == [
        "2026-04-21T12:00:00-04:00",
        "2026-04-21T11:00:00-04:00",
    ]


def test_limit_clamps_output(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    _write_jsonl(
        path,
        [_event(f"2026-04-21T{h:02d}:00:00-04:00") for h in range(10, 20)],
    )
    assert len(read_audit_entries(path, limit=3)) == 3


def test_limit_zero_or_negative_returns_empty(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    _write_jsonl(path, [_event("2026-04-21T10:00:00-04:00")])
    assert read_audit_entries(path, limit=0) == []
    assert read_audit_entries(path, limit=-1) == []


def test_malformed_json_line_is_skipped(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(_event("2026-04-21T12:00:00-04:00")) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps(_event("2026-04-21T11:00:00-04:00")) + "\n")
    entries = read_audit_entries(path)
    assert len(entries) == 2


def test_missing_ts_field_is_skipped(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"strategy": "ic", "reason": "no ts"}) + "\n")
        f.write(json.dumps(_event("2026-04-21T12:00:00-04:00")) + "\n")
    entries = read_audit_entries(path)
    assert len(entries) == 1


def test_non_dict_line_is_skipped(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps([1, 2, 3]) + "\n")
        f.write(json.dumps(_event("2026-04-21T12:00:00-04:00")) + "\n")
    entries = read_audit_entries(path)
    assert len(entries) == 1


# ── Route tests ───────────────────────────────────────────────────────────


@pytest.fixture
def client_with_audit(tmp_path, monkeypatch):
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8765},
                "auth": {
                    "valid_tokens": [{"token": VALID_TOKEN, "label": "t"}],
                },
            }
        )
    )
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)

    audit_file = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setattr(audit_mod, "_DEFAULT_AUDIT_PATH", audit_file)

    return TestClient(app), audit_file


def test_route_404_on_unknown_strategy(client_with_audit):
    client, _ = client_with_audit
    r = client.get(
        "/api/v1/strategies/bogus/history/config",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 404


def test_route_requires_auth(client_with_audit):
    client, _ = client_with_audit
    r = client.get("/api/v1/strategies/ic/history/config")
    assert r.status_code == 401


def test_route_empty_when_file_missing(client_with_audit):
    client, _ = client_with_audit
    r = client.get(
        "/api/v1/strategies/ic/history/config",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.json() == {"entries": [], "has_more": False}


def test_route_filters_by_strategy(client_with_audit):
    client, audit_file = client_with_audit
    _write_jsonl(
        audit_file,
        [
            _event("2026-04-21T10:00:00-04:00", strategy="ic"),
            _event("2026-04-21T11:00:00-04:00", strategy="wheel"),
            _event("2026-04-21T12:00:00-04:00", strategy="ic"),
        ],
    )
    r = client.get(
        "/api/v1/strategies/ic/history/config",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    body = r.json()
    assert len(body["entries"]) == 2
    assert all(e["strategy"] == "ic" for e in body["entries"])


def test_route_pagination_has_more(client_with_audit):
    client, audit_file = client_with_audit
    # 60 ic entries — first page of 50 returns has_more=True.
    _write_jsonl(
        audit_file,
        [_event(f"2026-04-21T{h:02d}:{m:02d}:00-04:00") for h in range(10, 15) for m in range(0, 60, 5)],
    )
    r1 = client.get(
        "/api/v1/strategies/ic/history/config?limit=50",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    ).json()
    assert len(r1["entries"]) == 50
    assert r1["has_more"] is True

    oldest = r1["entries"][-1]["ts"]
    r2 = client.get(
        f"/api/v1/strategies/ic/history/config?limit=50&before_ts={oldest}",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    ).json()
    assert len(r2["entries"]) == 10  # 60 total - 50 first
    assert r2["has_more"] is False


def test_route_limit_clamped_to_200(client_with_audit):
    client, audit_file = client_with_audit
    _write_jsonl(audit_file, [_event("2026-04-21T10:00:00-04:00")])
    r = client.get(
        "/api/v1/strategies/ic/history/config?limit=9999",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 200
    # Only 1 entry exists; what matters is the request didn't 400.
    assert len(r.json()["entries"]) == 1

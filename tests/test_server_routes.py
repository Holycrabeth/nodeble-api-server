"""Tests for /api/v1/server/* routes — Phase A Week 1 stubs + Week 3 wiring.

Per Phase 4.1 contract freeze
(`~/projects/cto/reviews/2026-04-26-phase-4.1-backend-contract-freeze.md`)
+ Phase A Week 3 wiring (Path C 5/5
`2026-05-05-path-c-saas-install-master-spec.md`).

What we lock down (shape + behavior):
  - 200/202/404/400 status codes per spec
  - response key presence + types
  - install_id idempotency (POST same id twice returns existing AND does
    NOT re-spawn subprocess)
  - mode=dry_run server-side enforcement (Gap 2 fix)
  - SSE event format (event: ... \\ndata: {...}\\n\\n)
  - auth required (401 without Bearer)
  - Phase A Week 3: ``post_install`` schedules ``install_runner.run_install``
    with correct argv (bash + deploy.sh + --non-interactive + config + flags)
  - Phase A Week 3: ``/logs/api-server`` invokes ``journalctl --user`` and
    parses JSON output; degrades gracefully when journalctl is missing
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nodeble_api_server import config, install_state, state_reader
from nodeble_api_server.app import app
from nodeble_api_server.routes import server as server_mod


VALID_TOKEN = "server-routes-test-token"


def _hdr() -> dict:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# Phase A Week 3 — captured args for tests that verify install_runner wiring.
# Each test gets a fresh list via the client fixture.
_RUN_INSTALL_INVOCATIONS: list[dict] = []


async def _noop_run_install(*, install_id, cmd, cwd=None, env=None,
                             total_budget_ms=600_000, home=None):
    """Test replacement for install_runner.run_install.

    Records its invocation args + writes a synthetic 'complete' event to
    events.jsonl so the SSE replay-then-tail generator can finish without
    waiting for a real subprocess.
    """
    _RUN_INSTALL_INVOCATIONS.append({
        "install_id": install_id,
        "cmd": list(cmd),
        "cwd": str(cwd) if cwd is not None else None,
    })
    install_state.update_state(
        install_id, status="success", completed_at="test-ts", home=home,
    )
    install_state.append_event(
        install_id,
        event_type="complete",
        payload={"status": "success", "duration_ms": 0, "ts": "test-ts"},
        home=home,
    )
    return {"status": "success", "duration_ms": 0, "ts": "test-ts"}


@pytest.fixture(autouse=True)
def _reset_install_state():
    """Clear in-memory install state between tests (stub state isn't persisted)."""
    server_mod._INSTALL_STATE.clear()
    server_mod._TIGER_CREDS_STUB = {"exists": False, "account": None, "stored_at": None}
    server_mod._RUNNING_INSTALL_TASKS.clear()
    _RUN_INSTALL_INVOCATIONS.clear()
    state_reader.clear_cache()
    yield
    server_mod._INSTALL_STATE.clear()
    server_mod._TIGER_CREDS_STUB = {"exists": False, "account": None, "stored_at": None}
    server_mod._RUNNING_INSTALL_TASKS.clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "api.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "server": {"host": "127.0.0.1", "port": 8765},
        "auth": {"valid_tokens": [{"token": VALID_TOKEN, "label": "t"}]},
    }))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # Phase A Week 3: replace install_runner.run_install with a no-op that
    # writes a synthetic complete event. Prevents tests from spawning real
    # bash subprocesses (which would fail anyway since deploy.sh paths are
    # under tmp_path/projects/* — empty).
    monkeypatch.setattr(server_mod.install_runner, "run_install", _noop_run_install)

    return TestClient(app)


# ── Auth ────────────────────────────────────────────────────────────────────


def test_installed_strategies_requires_auth(client):
    r = client.get("/api/v1/server/installed-strategies")
    assert r.status_code == 401


def test_install_requires_auth(client):
    r = client.post("/api/v1/server/install/wheel", json={"install_id": "x", "config": {}})
    assert r.status_code == 401


# ── Discovery ───────────────────────────────────────────────────────────────


def test_installed_strategies_returns_9_strategies(client):
    """All 9 from STRATEGY_REGISTRY appear with shape per §1.1."""
    r = client.get("/api/v1/server/installed-strategies", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert "strategies" in body
    assert "fetched_at" in body
    cards = body["strategies"]
    assert len(cards) == 9

    expected_ids = {
        "ic", "wheel", "pmcc", "directionalspread", "calendar",
        "collar", "ironbutterfly", "straddle", "strangle",
    }
    assert {c["id"] for c in cards} == expected_ids

    # Schema check on each card
    for card in cards:
        assert set(card.keys()) >= {
            "id", "name", "installed", "status", "installed_at",
            "version", "latest_version_available",
        }
        assert isinstance(card["installed"], bool)
        assert card["status"] in ("running", "paused", "installing", "failed", "not_installed")


def test_strategy_versions_returns_manifest_skeleton(client):
    """Stub returns 9-strategy manifest with placeholder versions."""
    r = client.get("/api/v1/server/strategy-versions", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["manifest_url"] == "https://nodeble.app/releases.json"
    assert body["manifest_unreachable"] is False
    assert "fetched_at" in body
    assert len(body["strategies"]) == 9

    # Each strategy has latest + released_at + changelog_url
    for sid, info in body["strategies"].items():
        assert "latest" in info
        assert "released_at" in info
        assert "changelog_url" in info


# ── Lifecycle: install ──────────────────────────────────────────────────────


def _put_creds(client) -> None:
    """Helper: store dummy Tiger creds via PUT before POST /install.

    Mirrors the realistic customer flow (Step 1 PUT creds → Step 2 POST install)
    and satisfies the reuse_tiger_creds=true gate added 2026-04-29 (UI 总监
    Bug 1 fix: must 422 if reuse=true and no creds present).
    """
    r = client.put(
        "/api/v1/server/credentials/tiger",
        headers=_hdr(),
        json={
            "tiger_id": "test-tiger-id",
            "tiger_account": "U1234567",
            "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        },
    )
    assert r.status_code == 200


def test_install_returns_202_with_urls(client):
    """POST /install returns 202 + sse_url + status_url + log_url."""
    _put_creds(client)
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "test-uuid-1", "config": {"budget": 30000}},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["install_id"] == "test-uuid-1"
    assert body["status"] in ("queued", "starting")
    assert body["sse_url"] == "/api/v1/server/install/test-uuid-1/stream"
    assert body["status_url"] == "/api/v1/server/install/test-uuid-1/status"
    assert body["log_url"] == "/api/v1/server/install/test-uuid-1/log"
    assert "started_at" in body


def test_install_404_unknown_strategy(client):
    r = client.post(
        "/api/v1/server/install/nonexistent",
        headers=_hdr(),
        json={"install_id": "x", "config": {}},
    )
    assert r.status_code == 404


def test_install_idempotent_same_install_id(client):
    """Two POSTs with same install_id return same install (no double-spawn)."""
    _put_creds(client)
    body = {"install_id": "test-uuid-2", "config": {}}
    r1 = client.post("/api/v1/server/install/wheel", headers=_hdr(), json=body)
    assert r1.status_code == 202
    r2 = client.post("/api/v1/server/install/wheel", headers=_hdr(), json=body)
    # Same install_id returns 200/202 with same install_id
    assert r2.status_code in (200, 202)
    assert r2.json()["install_id"] == "test-uuid-2"


def test_install_enforces_dry_run_mode_server_side(client):
    """Even if frontend POSTs config={mode: live}, backend must override to dry_run.
    Per Phase 4.1 contract §1.2 + UI 总监 Gap 2 fix.
    """
    _put_creds(client)
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={
            "install_id": "test-mode-enforce",
            "config": {"mode": "live", "budget": 30000},  # malicious / mistaken frontend
        },
    )
    assert r.status_code == 202
    state = server_mod._INSTALL_STATE["test-mode-enforce"]
    enforced = state["config_with_mode_dry_run_enforced"]
    assert enforced["mode"] == "dry_run", "backend MUST override mode to dry_run"


def test_install_422_when_reuse_creds_true_but_creds_missing(client):
    """UI 总监 Bug 1 fix (2026-04-29 audit): reuse_tiger_creds=true + creds NOT
    on disk MUST return 422 fail-fast — NOT 202 with subprocess that will fail
    30s later at "Resolving Tiger credentials" step. Saves user 30s of bad UX.

    Spec amendment to freeze line 144 (was: "422: Tiger creds missing AND
    reuse_tiger_creds: false" — also covers reuse_tiger_creds: true + creds
    not on disk).
    """
    # Note: NO _put_creds(client) call — creds intentionally absent
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={
            "install_id": "test-bug1-422",
            "config": {"budget": 30000},
            "reuse_tiger_creds": True,
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert "tiger" in body["detail"].lower() or "creds" in body["detail"].lower(), (
        f"422 detail should mention Tiger creds; got: {body['detail']}"
    )
    # Verify NO install state was created — gate must fire before install_state.create()
    assert "test-bug1-422" not in server_mod._INSTALL_STATE


def test_install_202_when_reuse_creds_true_and_creds_present(client):
    """Bug 1 control: reuse_tiger_creds=true with creds PUT first → 202 (normal flow)."""
    _put_creds(client)
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={
            "install_id": "test-bug1-control",
            "config": {"budget": 30000},
            "reuse_tiger_creds": True,
        },
    )
    assert r.status_code == 202


def test_install_default_reuse_creds_is_true_so_gate_applies(client):
    """Bug 1 default behavior: omitting reuse_tiger_creds defaults to True per
    InstallRequest schema, so gate must apply same as explicit true.
    """
    # NO creds PUT — should still 422 even though reuse_tiger_creds is omitted
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "test-bug1-default", "config": {}},
    )
    assert r.status_code == 422


# ── Lifecycle: validate ─────────────────────────────────────────────────────


def test_validate_returns_valid_true(client):
    r = client.post(
        "/api/v1/server/install/wheel/validate",
        headers=_hdr(),
        json={"config": {"budget": 30000}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["warnings"] == []


def test_validate_404_unknown_strategy(client):
    r = client.post(
        "/api/v1/server/install/nonexistent/validate",
        headers=_hdr(),
        json={"config": {}},
    )
    assert r.status_code == 404


# ── Lifecycle: uninstall / update / pause / resume ──────────────────────────


def test_uninstall_returns_uninstalled_status(client):
    r = client.post("/api/v1/server/uninstall/wheel", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "uninstalled"
    assert "uninstalled_at" in body
    assert "state_archive_path" in body


def test_update_returns_202_like_install(client):
    r = client.post(
        "/api/v1/server/update/wheel",
        headers=_hdr(),
        json={"install_id": "update-uuid-1"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["install_id"] == "update-uuid-1"
    assert body["status"] == "queued"
    assert "sse_url" in body


def test_pause_returns_paused_with_cron_disabled(client):
    r = client.post("/api/v1/server/pause/wheel", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert "paused_at" in body
    assert "signal" in body["cron_disabled"]
    assert "scan" in body["cron_disabled"]


def test_resume_returns_running(client):
    r = client.post("/api/v1/server/resume/wheel", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert "resumed_at" in body


# ── Tiger creds ─────────────────────────────────────────────────────────────


def test_get_tiger_creds_initially_absent(client):
    r = client.get("/api/v1/server/credentials/tiger", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is False
    assert body["account"] is None
    assert body["stored_at"] is None


def test_put_tiger_creds_then_get_shows_account(client):
    r = client.put(
        "/api/v1/server/credentials/tiger",
        headers=_hdr(),
        json={
            "tiger_id": "50691693",
            "tiger_account": "Yongtao_2K1",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE_KEY\n-----END PRIVATE KEY-----",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "stored"
    assert r.json()["account"] == "Yongtao_2K1"

    # GET should now show exists=true with account
    r2 = client.get("/api/v1/server/credentials/tiger", headers=_hdr())
    body = r2.json()
    assert body["exists"] is True
    assert body["account"] == "Yongtao_2K1"
    # CRITICAL: never returns private_key_pem
    assert "private_key_pem" not in body


def test_get_tiger_creds_never_leaks_private_key(client):
    """Security regression guard — even after PUT, GET response must never
    include the private key."""
    client.put(
        "/api/v1/server/credentials/tiger",
        headers=_hdr(),
        json={
            "tiger_id": "x",
            "tiger_account": "y",
            "private_key_pem": "SECRET_PEM_HERE_DO_NOT_LEAK",
        },
    )
    r = client.get("/api/v1/server/credentials/tiger", headers=_hdr())
    serialized = json.dumps(r.json())
    assert "SECRET_PEM_HERE" not in serialized
    assert "private_key_pem" not in serialized


# ── Install observability ───────────────────────────────────────────────────


def test_install_status_404_unknown(client):
    r = client.get("/api/v1/server/install/unknown-id/status", headers=_hdr())
    assert r.status_code == 404


def test_install_status_returns_state_after_install(client):
    """POST /install then GET /status — should see the state."""
    _put_creds(client)
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "status-test-1", "config": {}},
    )
    r = client.get("/api/v1/server/install/status-test-1/status", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["install_id"] == "status-test-1"
    assert body["status"] in ("queued", "running", "success", "failed")
    assert "current_step" in body
    assert isinstance(body["steps_completed"], list)
    assert isinstance(body["log_tail"], list)


def test_install_log_404_unknown(client):
    r = client.get("/api/v1/server/install/unknown-id/log", headers=_hdr())
    assert r.status_code == 404


def test_install_log_returns_text_plain(client):
    _put_creds(client)
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "log-test-1", "config": {}},
    )
    r = client.get("/api/v1/server/install/log-test-1/log", headers=_hdr())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # Stub log has at least the queued message
    assert "queued" in r.text.lower() or "stub" in r.text.lower()


def test_install_stream_returns_sse_content_type(client):
    """SSE endpoint sets text/event-stream + emits valid event format."""
    _put_creds(client)
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "stream-test-1", "config": {}},
    )
    # TestClient consumes streaming response — we read full body
    with client.stream("GET", "/api/v1/server/install/stream-test-1/stream", headers=_hdr()) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > 200:  # got enough to check format
                break
        text = body.decode("utf-8", errors="replace")
        # Per Phase 4.1 contract §2.1 — events have format `event: X\ndata: {...}\n\n`
        assert "event:" in text
        assert "data:" in text


def test_install_stream_unknown_id_emits_failed_complete(client):
    """SSE for unknown install_id emits a 'complete' event with failed status
    rather than 404 (per spec — late subscribers tolerate)."""
    with client.stream("GET", "/api/v1/server/install/never-existed/stream", headers=_hdr()) as r:
        assert r.status_code == 200
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
        text = body.decode("utf-8", errors="replace")
        assert "complete" in text
        assert "failed" in text or "not found" in text


# ── Server logs ─────────────────────────────────────────────────────────────


def test_server_logs_returns_lines(client):
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert "lines" in body
    assert isinstance(body["lines"], list)
    assert "total_returned" in body
    assert "source" in body


def test_server_logs_400_invalid_lines_count(client):
    r = client.get("/api/v1/server/logs/api-server?lines=999", headers=_hdr())
    assert r.status_code == 400


def test_server_logs_400_invalid_level(client):
    r = client.get("/api/v1/server/logs/api-server?level=panic", headers=_hdr())
    assert r.status_code == 400


def test_server_logs_filter_by_level(client):
    """level=error filters to only error-level lines."""
    r = client.get("/api/v1/server/logs/api-server?level=info", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    for line in body["lines"]:
        assert line["level"] == "info"


# ── Phase A Week 3 wiring tests (install_runner spawn + journalctl) ─────────


def test_install_spawns_install_runner_with_correct_cmd(client, tmp_path):
    """POST /install schedules install_runner.run_install with bash + deploy.sh
    --non-interactive --config <path> + extra args."""
    _put_creds(client)
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "phase-a-w3-1", "config": {"capital_usd": 50000}},
    )
    assert r.status_code == 202
    # The no-op fixture captures invocations
    assert len(_RUN_INSTALL_INVOCATIONS) == 1
    inv = _RUN_INSTALL_INVOCATIONS[0]
    assert inv["install_id"] == "phase-a-w3-1"
    assert inv["cmd"][0] == "bash"
    assert inv["cmd"][1].endswith("/deploy/deploy.sh")
    # repo_dir for wheel is "projects/nodeble-wheel" per state_reader.STRATEGY_REGISTRY
    assert "nodeble-wheel" in inv["cmd"][1]
    assert "--non-interactive" in inv["cmd"]
    assert "--config" in inv["cmd"]
    # No telegram in payload → --skip-telegram appended
    assert "--skip-telegram" in inv["cmd"]


def test_install_telegram_present_omits_skip_flag(client):
    """payload.telegram set → don't append --skip-telegram."""
    _put_creds(client)
    r = client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={
            "install_id": "phase-a-w3-tg",
            "config": {},
            "telegram": {"bot_token": "x", "chat_id": "y"},
        },
    )
    assert r.status_code == 202
    inv = _RUN_INSTALL_INVOCATIONS[0]
    assert "--skip-telegram" not in inv["cmd"]


def test_install_idempotent_does_not_double_spawn(client):
    """Two POSTs with same install_id → install_runner.run_install called ONCE."""
    _put_creds(client)
    body = {"install_id": "phase-a-w3-idem", "config": {}}
    r1 = client.post("/api/v1/server/install/wheel", headers=_hdr(), json=body)
    assert r1.status_code == 202
    assert len(_RUN_INSTALL_INVOCATIONS) == 1
    # Second POST: same install_id, same shape — no new subprocess spawn
    r2 = client.post("/api/v1/server/install/wheel", headers=_hdr(), json=body)
    assert r2.status_code == 202
    assert len(_RUN_INSTALL_INVOCATIONS) == 1, (
        "idempotency violated — same install_id re-spawned subprocess"
    )


def test_install_writes_config_json_to_install_dir(client, tmp_path):
    """post_install writes payload.config (with mode=dry_run injected) to
    ``<install_dir>/config.json`` for deploy.sh --config."""
    _put_creds(client)
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "phase-a-w3-cfg", "config": {"capital_usd": 50000}},
    )
    cfg_path = tmp_path / ".nodeble-api" / "data" / "installs" / "phase-a-w3-cfg" / "config.json"
    assert cfg_path.exists()
    written = json.loads(cfg_path.read_text())
    assert written["mode"] == "dry_run"  # server-side enforcement
    assert written["capital_usd"] == 50000


def test_update_spawns_install_runner_with_skip_telegram(client):
    """POST /update reuses install_runner; no telegram by default."""
    r = client.post(
        "/api/v1/server/update/wheel",
        headers=_hdr(),
        json={"install_id": "phase-a-w3-upd", "target_version": "0.7.3"},
    )
    assert r.status_code == 202
    assert len(_RUN_INSTALL_INVOCATIONS) == 1
    inv = _RUN_INSTALL_INVOCATIONS[0]
    assert inv["install_id"] == "phase-a-w3-upd"
    assert "--skip-telegram" in inv["cmd"]


def test_install_stream_replays_complete_event_from_jsonl(client):
    """SSE generator yields the no-op-fixture's complete event."""
    _put_creds(client)
    client.post(
        "/api/v1/server/install/wheel",
        headers=_hdr(),
        json={"install_id": "phase-a-w3-sse", "config": {}},
    )
    # No-op fixture's run_install completes synchronously, writes complete event
    with client.stream(
        "GET", "/api/v1/server/install/phase-a-w3-sse/stream", headers=_hdr(),
    ) as r:
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if b"complete" in body:
                break
        text = body.decode()
    assert "event: complete" in text
    assert "success" in text


# ── /logs/api-server (journalctl wiring) ────────────────────────────────────


def test_logs_api_server_journalctl_unavailable_returns_empty(client, monkeypatch):
    """When journalctl binary not on PATH → 200 with empty lines + tagged source."""
    monkeypatch.setattr(server_mod.shutil, "which", lambda _: None)
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["lines"] == []
    assert body["source"] == "journalctl_unavailable"


def test_logs_api_server_journalctl_success_passthrough(client, monkeypatch):
    """journalctl returns JSON-stream output → parsed into {ts, level, message}."""
    sample = (
        '{"__REALTIME_TIMESTAMP":"1714900000000000","MESSAGE":"hello","PRIORITY":"6"}\n'
        '{"__REALTIME_TIMESTAMP":"1714900001000000","MESSAGE":"warn-line","PRIORITY":"4"}\n'
        '{"__REALTIME_TIMESTAMP":"1714900002000000","MESSAGE":"err-line","PRIORITY":"3"}\n'
    )

    def fake_run(args, **kw):
        # Verify cmd shape
        assert args[0] == "journalctl"
        assert "--user" in args
        assert "-u" in args
        assert "-o" in args and "json" in args
        return subprocess.CompletedProcess(args, 0, stdout=sample, stderr="")

    monkeypatch.setattr(server_mod.shutil, "which", lambda _: "/usr/bin/journalctl")
    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "journalctl_user"
    assert body["total_returned"] == 3
    levels = [line["level"] for line in body["lines"]]
    assert levels == ["info", "warn", "error"]


def test_logs_api_server_invokes_priority_flag_for_level(client, monkeypatch):
    """level=warn → journalctl args include `-p warning`."""
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(server_mod.shutil, "which", lambda _: "/usr/bin/journalctl")
    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    client.get("/api/v1/server/logs/api-server?level=warn", headers=_hdr())
    assert "-p" in captured["args"]
    p_idx = captured["args"].index("-p")
    assert captured["args"][p_idx + 1] == "warning"


def test_logs_api_server_timeout_504(client, monkeypatch):
    monkeypatch.setattr(server_mod.shutil, "which", lambda _: "/usr/bin/journalctl")

    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, 5)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 504


def test_logs_api_server_subprocess_nonzero_500(client, monkeypatch):
    monkeypatch.setattr(server_mod.shutil, "which", lambda _: "/usr/bin/journalctl")

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unit not found")

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 500
    assert "unit not found" in r.json()["detail"]


def test_logs_api_server_parse_record_handles_missing_fields(client, monkeypatch):
    """Defensive: journalctl record without PRIORITY → defaults to info."""
    sample = '{"__REALTIME_TIMESTAMP":"1714900000000000","MESSAGE":"hi"}\n'
    monkeypatch.setattr(server_mod.shutil, "which", lambda _: "/usr/bin/journalctl")
    monkeypatch.setattr(
        server_mod.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=sample, stderr=""),
    )
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["lines"][0]["level"] == "info"
    assert body["lines"][0]["message"] == "hi"


def test_logs_api_server_non_json_line_falls_back_to_logs_parser(client, monkeypatch):
    """Defensive: non-JSON line → reuse logs.parse_log_line for free-form fallback."""
    sample = "not json at all — just bare text\n"
    monkeypatch.setattr(server_mod.shutil, "which", lambda _: "/usr/bin/journalctl")
    monkeypatch.setattr(
        server_mod.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=sample, stderr=""),
    )
    r = client.get("/api/v1/server/logs/api-server", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    # logs.parse_log_line returns None for unparseable; fallback uses raw text
    assert len(body["lines"]) == 1
    assert "not json" in body["lines"][0]["message"]

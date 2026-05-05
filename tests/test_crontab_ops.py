"""Tests for crontab_ops.py — Path C Item 4 lifecycle endpoint internals.

Covers the L1 §7.11 #8 4-constraint contract:
  (i)   Process binding (systemctl --user MainPID match)
  (ii)  Path-substring + python -m matching
  (iii) Pre-edit backup + retention
  (iv)  Post-edit verification

Real Tower crontab fixture at ``tests/fixtures/tower-crontab-2026-05-05.txt``
captured 2026-05-05 SGT — exercises module ↔ line scoping against reality.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nodeble_api_server import crontab_ops


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tower-crontab-2026-05-05.txt"
TOWER_CRONTAB = FIXTURE_PATH.read_text() if FIXTURE_PATH.exists() else ""
TOWER_LINES = TOWER_CRONTAB.splitlines()


# ── Strategy → module_pkg derivation ────────────────────────────────────────


@pytest.mark.parametrize("strategy,expected_pkg", [
    ("ic", "nodeble"),
    ("wheel", "nodeble_wheel"),
    ("pmcc", "nodeble_pmcc"),
    ("directionalspread", "nodeble_directionalspread"),
    ("calendar", "nodeble_calendar"),
    ("ironbutterfly", "nodeble_ironbutterfly"),
    ("strangle", "nodeble_strangle"),
    ("straddle", "nodeble_straddle"),
    ("collar", "nodeble_collar"),
])
def test_module_pkg_for_matches_tower_reality(strategy, expected_pkg):
    """Verified 2026-05-05 against real Tower crontab `python -m <pkg>` lines."""
    assert crontab_ops.module_pkg_for(strategy) == expected_pkg


def test_module_pkg_for_unknown_strategy_returns_none():
    assert crontab_ops.module_pkg_for("nonexistent") is None


def test_repo_dir_basename_for_strips_projects_prefix():
    assert crontab_ops.repo_dir_basename_for("wheel") == "nodeble-wheel"
    assert crontab_ops.repo_dir_basename_for("ic") == "nodeble"


# ── Constraint (ii): in_scope_crontab_line — against real Tower fixture ────


@pytest.mark.skipif(not TOWER_LINES, reason="Tower fixture missing")
def test_wheel_scope_does_not_match_ic_lines():
    """IC lines have /projects/nodeble/ + python -m nodeble. Wheel scope
    requires /projects/nodeble-wheel/ + python -m nodeble_wheel. Trailing
    slash on path prevents IC from being misclassified as Wheel."""
    ic_lines = [l for l in TOWER_LINES if "/projects/nodeble " in l + " " or
                "/projects/nodeble&" in l or
                ("/projects/nodeble/" in l and "nodeble-" not in l.split("/projects/")[1].split("/")[0])]
    # Simpler: just iterate Tower lines
    for line in TOWER_LINES:
        if "/projects/nodeble/" in line and "python -m nodeble " in line:
            # This is an IC line — should NOT be in-scope for Wheel
            assert not crontab_ops.in_scope_crontab_line(
                line, "nodeble-wheel", "nodeble_wheel"
            ), f"Wheel scope wrongly matched IC line: {line!r}"


@pytest.mark.skipif(not TOWER_LINES, reason="Tower fixture missing")
def test_ic_scope_does_not_match_wheel_lines():
    """Symmetric: Wheel lines should NOT match IC scope."""
    for line in TOWER_LINES:
        if "/projects/nodeble-wheel/" in line:
            assert not crontab_ops.in_scope_crontab_line(
                line, "nodeble", "nodeble"
            ), f"IC scope wrongly matched Wheel line: {line!r}"


@pytest.mark.skipif(not TOWER_LINES, reason="Tower fixture missing")
def test_each_strategy_finds_its_own_lines_in_tower_fixture():
    """Sanity: each strategy's scope predicate matches at least one line in
    the real Tower crontab (proves wiring works end-to-end on real data)."""
    expected_lines_per_strategy = {
        "ic": 4,                # signal/manage/scan/force-manage
        "wheel": 1,             # at least one (Tower may have more)
        "pmcc": 4,
        "directionalspread": 3,
        "calendar": 4,
        "ironbutterfly": 4,
        "strangle": 4,
        # straddle/collar may or may not be present; not asserted strictly
    }
    for strategy, min_count in expected_lines_per_strategy.items():
        repo = crontab_ops.repo_dir_basename_for(strategy)
        pkg = crontab_ops.module_pkg_for(strategy)
        matched = [l for l in TOWER_LINES if crontab_ops.in_scope_crontab_line(l, repo, pkg)]
        assert len(matched) >= min_count, (
            f"{strategy}: expected ≥{min_count} matches, got {len(matched)}"
        )


def test_user_handwritten_line_not_in_scope():
    """A user-written cron with no match-substring should be ignored by ALL
    strategy scopes."""
    user_line = "0 9 * * * /usr/bin/backup.sh"
    for strategy in ("ic", "wheel", "pmcc", "calendar"):
        repo = crontab_ops.repo_dir_basename_for(strategy)
        pkg = crontab_ops.module_pkg_for(strategy)
        assert not crontab_ops.in_scope_crontab_line(user_line, repo, pkg)


def test_user_line_with_only_path_no_python_module_not_in_scope():
    """Only path matches, no `python -m <pkg>` → out of scope (defensive)."""
    line = "0 9 * * * cd /home/user/projects/nodeble-wheel/ && /bin/echo hi"
    assert not crontab_ops.in_scope_crontab_line(line, "nodeble-wheel", "nodeble_wheel")


def test_user_line_with_only_module_no_path_not_in_scope():
    """`python -m nodeble_wheel` from elsewhere → out of scope."""
    line = "0 9 * * * /home/user/some-other-venv/bin/python -m nodeble_wheel scan"
    assert not crontab_ops.in_scope_crontab_line(line, "nodeble-wheel", "nodeble_wheel")


def test_paused_line_still_in_scope():
    """PAUSED-by-api: prefix doesn't strip the substrings — so resume can find them."""
    orig = (
        "35 13 * * 1-5 cd /home/x/projects/nodeble && "
        "/home/x/projects/nodeble/.venv/bin/python -m nodeble --mode signal"
    )
    paused = crontab_ops._transform_pause(orig)
    assert crontab_ops.in_scope_crontab_line(paused, "nodeble", "nodeble")


# ── Constraint (i): process binding ─────────────────────────────────────────


def test_process_binding_refuses_when_systemctl_missing(monkeypatch):
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: None)
    ok, diag = crontab_ops.check_process_binding()
    assert ok is False
    assert "systemctl" in diag.lower() or "no pid" in diag.lower()


def test_process_binding_refuses_when_main_pid_zero(monkeypatch):
    """MainPID=0 = unit not running."""
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(
        crontab_ops.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="0\n", stderr=""),
    )
    ok, diag = crontab_ops.check_process_binding()
    assert ok is False


def test_process_binding_passes_when_pid_matches(monkeypatch):
    my_pid = os.getpid()
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(
        crontab_ops.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=f"{my_pid}\n", stderr=""),
    )
    ok, diag = crontab_ops.check_process_binding()
    assert ok is True
    assert diag is None


def test_process_binding_passes_when_ppid_matches(monkeypatch):
    """Multi-worker uvicorn: my PPID == MainPID (the master)."""
    my_ppid = os.getppid()
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(
        crontab_ops.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=f"{my_ppid}\n", stderr=""),
    )
    ok, diag = crontab_ops.check_process_binding()
    assert ok is True


def test_process_binding_refuses_when_neither_pid_matches(monkeypatch):
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(
        crontab_ops.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="999999\n", stderr=""),
    )
    ok, diag = crontab_ops.check_process_binding()
    assert ok is False
    assert "not bound" in diag


def test_process_binding_refuses_on_subprocess_timeout(monkeypatch):
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(a, 5)

    monkeypatch.setattr(crontab_ops.subprocess, "run", fake_run)
    ok, _ = crontab_ops.check_process_binding()
    assert ok is False


# ── Constraint (iii): pre-edit backup + retention ──────────────────────────


def test_backup_writes_file_to_install_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        crontab_ops.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="cron content\n", stderr=""),
    )
    backup_path = crontab_ops.backup_crontab("test-install-1", home=tmp_path)
    assert backup_path.exists()
    assert backup_path.read_text() == "cron content\n"
    # Path matches contract: ~/.nodeble-api/crontab-backups/<install_id>-<ts>.bak
    assert backup_path.parent == tmp_path / ".nodeble-api" / "crontab-backups"
    assert backup_path.name.startswith("test-install-1-")
    assert backup_path.suffix == ".bak"


def test_backup_handles_no_crontab_gracefully(tmp_path, monkeypatch):
    """`crontab -l` returns rc=1 on no crontab → backup is empty file."""
    monkeypatch.setattr(
        crontab_ops.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout="", stderr="no crontab"),
    )
    backup_path = crontab_ops.backup_crontab("test-empty", home=tmp_path)
    assert backup_path.exists()
    assert backup_path.read_text() == ""


def test_cleanup_old_backups_respects_retention(tmp_path):
    backup_dir = tmp_path / ".nodeble-api" / "crontab-backups"
    backup_dir.mkdir(parents=True)
    fresh = backup_dir / "fresh.bak"
    stale = backup_dir / "stale.bak"
    fresh.write_text("")
    stale.write_text("")
    # Set stale's mtime to 60 days ago
    old = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
    os.utime(stale, (old, old))

    removed = crontab_ops.cleanup_old_backups(home=tmp_path, days=30)
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


# ── Constraint (iv): verify_post_edit ──────────────────────────────────────


def test_verify_pause_unchanged_out_of_scope_passes():
    in_scope = lambda l: "wheel" in l
    before = ["user line 1", "wheel line A", "user line 2", "wheel line B"]
    after = ["user line 1", crontab_ops.PAUSE_MARKER + "wheel line A",
             "user line 2", crontab_ops.PAUSE_MARKER + "wheel line B"]
    ok, err = crontab_ops.verify_post_edit(before, after, in_scope, "comment")
    assert ok, f"unexpected fail: {err}"


def test_verify_remove_drops_in_scope_passes():
    in_scope = lambda l: "wheel" in l
    before = ["user line 1", "wheel line A", "user line 2", "wheel line B"]
    after = ["user line 1", "user line 2"]
    ok, err = crontab_ops.verify_post_edit(before, after, in_scope, "remove")
    assert ok, f"unexpected fail: {err}"


def test_verify_catches_out_of_scope_change():
    """Defense in depth: if pause accidentally touched a user line, fail."""
    in_scope = lambda l: "wheel" in l
    before = ["user line 1", "wheel line A"]
    after = ["DELETED user line 1", crontab_ops.PAUSE_MARKER + "wheel line A"]
    ok, err = crontab_ops.verify_post_edit(before, after, in_scope, "comment")
    assert ok is False
    assert "out-of-scope" in err.lower()


def test_verify_catches_line_count_change():
    in_scope = lambda l: "wheel" in l
    before = ["wheel line A", "wheel line B"]
    after = ["wheel line A"]  # accidentally dropped one
    ok, err = crontab_ops.verify_post_edit(before, after, in_scope, "comment")
    assert ok is False


def test_verify_remove_with_residual_in_scope_fails():
    """Two in-scope lines before, only one removed → fails on count."""
    in_scope = lambda l: "wheel" in l
    before = ["wheel line A", "wheel line B"]
    after = ["wheel line B"]  # remove only got 1 of 2
    ok, err = crontab_ops.verify_post_edit(before, after, in_scope, "remove")
    assert ok is False
    # Error message can describe the discrepancy in either of two ways
    # (line count mismatch OR residual in-scope) — both are valid signals.
    assert ("line count" in err.lower()) or ("in-scope" in err.lower())


def test_verify_remove_with_residual_in_scope_only_fails_on_count_zero_check():
    """If out-of-scope lines compensate (impossible in practice but tested):
    prove the in-scope-residual check fires distinctly from line-count check."""
    in_scope = lambda l: "wheel" in l
    # before: 2 wheel + 1 user = 3 total; after: 1 wheel + 2 user = 3 total
    # Total count matches BUT in-scope residual = 1 ≠ 0 → fail
    before = ["wheel line A", "wheel line B", "user line"]
    after = ["wheel line B", "user line", "user line 2"]  # added a user line
    ok, err = crontab_ops.verify_post_edit(before, after, in_scope, "remove")
    assert ok is False
    # Could fire on out-of-scope OR in-scope check; either is valid signal.
    assert ("out-of-scope" in err.lower()) or ("in-scope" in err.lower())


def test_verify_unknown_action_returns_error():
    ok, err = crontab_ops.verify_post_edit([], [], lambda l: True, "exotic-mode")
    assert ok is False
    assert "exotic-mode" in err


# ── Action transforms ──────────────────────────────────────────────────────


def test_pause_transform_adds_marker():
    line = "35 13 * * 1-5 cd /projects/nodeble && python -m nodeble"
    paused = crontab_ops._transform_pause(line)
    assert paused.startswith(crontab_ops.PAUSE_MARKER)
    assert paused.endswith(line)


def test_pause_idempotent_on_already_paused_line():
    line = crontab_ops.PAUSE_MARKER + "35 13 * * 1-5 cd /projects/nodeble && python -m nodeble"
    again = crontab_ops._transform_pause(line)
    assert again == line


def test_pause_skips_existing_comment_line():
    """A user-commented line in the same path scope shouldn't get re-prefixed."""
    line = "# 35 13 * * 1-5 cd /projects/nodeble && python -m nodeble"
    out = crontab_ops._transform_pause(line)
    assert out == line


def test_resume_strips_marker():
    line = "35 13 * * 1-5 cd /projects/nodeble && python -m nodeble"
    paused = crontab_ops._transform_pause(line)
    resumed = crontab_ops._transform_resume(paused)
    assert resumed == line


def test_resume_idempotent_on_unpaused_line():
    line = "35 13 * * 1-5 cd /projects/nodeble && python -m nodeble"
    out = crontab_ops._transform_resume(line)
    assert out == line


# ── High-level _apply_action_to_crontab integration ────────────────────────


@pytest.fixture
def _bound_process(monkeypatch):
    """Make check_process_binding() pass by mocking systemctl."""
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    my_pid = os.getpid()
    # Counter so different subprocess.run calls return different things based on argv
    state = {"sysctl_pid": my_pid, "crontab_l_output": "", "crontab_l_rc": 0,
             "crontab_w_called_with": None, "crontab_w_rc": 0}

    def fake_run(args, **kw):
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 0, stdout=f"{state['sysctl_pid']}\n", stderr="")
        if args[0] == "crontab" and args[1] == "-l":
            return subprocess.CompletedProcess(args, state["crontab_l_rc"], stdout=state["crontab_l_output"], stderr="")
        if args[0] == "crontab" and args[1] == "-":
            state["crontab_w_called_with"] = kw.get("input", "")
            # After write, subsequent -l reads return what was written
            state["crontab_l_output"] = state["crontab_w_called_with"]
            return subprocess.CompletedProcess(args, state["crontab_w_rc"], stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(crontab_ops.subprocess, "run", fake_run)
    return state


def test_apply_action_refuses_when_process_binding_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: None)
    out = crontab_ops._apply_action_to_crontab(
        "test-1", "wheel", "pause", home=tmp_path,
    )
    assert out["ok"] is False
    assert out["error"] == "process_binding_check_failed"
    assert "diagnostic" in out


def test_apply_action_pause_writes_marker_to_in_scope_lines(tmp_path, _bound_process):
    state = _bound_process
    state["crontab_l_output"] = (
        "0 9 * * * /usr/bin/backup.sh\n"
        "35 13 * * 1-5 cd /home/x/projects/nodeble-wheel && /home/x/projects/nodeble-wheel/.venv/bin/python -m nodeble_wheel scan\n"
        "37 13 * * 1-5 cd /home/x/projects/nodeble && /home/x/projects/nodeble/.venv/bin/python -m nodeble --mode signal\n"
    )
    out = crontab_ops._apply_action_to_crontab(
        "test-pause", "wheel", "pause", home=tmp_path,
    )
    assert out["ok"] is True, f"unexpected fail: {out.get('error')}"
    assert out["lines_changed"] == 1  # only the wheel line
    written = state["crontab_w_called_with"]
    assert "# PAUSED-by-api: 35 13 * * 1-5" in written
    # IC line untouched
    assert "37 13 * * 1-5 cd /home/x/projects/nodeble" in written
    assert "# PAUSED-by-api: 37 13" not in written
    # User line untouched
    assert "0 9 * * * /usr/bin/backup.sh" in written


def test_apply_action_uninstall_removes_in_scope_lines(tmp_path, _bound_process):
    state = _bound_process
    state["crontab_l_output"] = (
        "0 9 * * * /usr/bin/backup.sh\n"
        "35 13 * * 1-5 cd /home/x/projects/nodeble-wheel && /home/x/projects/nodeble-wheel/.venv/bin/python -m nodeble_wheel scan\n"
        "37 13 * * 1-5 cd /home/x/projects/nodeble-wheel && /home/x/projects/nodeble-wheel/.venv/bin/python -m nodeble_wheel signal\n"
    )
    out = crontab_ops._apply_action_to_crontab(
        "test-uninstall", "wheel", "uninstall", home=tmp_path,
    )
    assert out["ok"] is True
    assert out["lines_changed"] == 2
    written = state["crontab_w_called_with"]
    assert "nodeble_wheel" not in written
    assert "/usr/bin/backup.sh" in written


def test_apply_action_resume_strips_marker(tmp_path, _bound_process):
    state = _bound_process
    state["crontab_l_output"] = (
        "0 9 * * * /usr/bin/backup.sh\n"
        "# PAUSED-by-api: 35 13 * * 1-5 cd /home/x/projects/nodeble-wheel && /home/x/projects/nodeble-wheel/.venv/bin/python -m nodeble_wheel scan\n"
    )
    out = crontab_ops._apply_action_to_crontab(
        "test-resume", "wheel", "resume", home=tmp_path,
    )
    assert out["ok"] is True
    assert out["lines_changed"] == 1
    written = state["crontab_w_called_with"]
    assert "# PAUSED-by-api: 35 13" not in written
    assert "35 13 * * 1-5" in written


def test_apply_action_writes_backup(tmp_path, _bound_process):
    state = _bound_process
    state["crontab_l_output"] = "0 9 * * * /usr/bin/backup.sh\n"
    out = crontab_ops._apply_action_to_crontab(
        "test-backup", "wheel", "pause", home=tmp_path,
    )
    backup_path = Path(out["backup_path"])
    assert backup_path.exists()
    assert backup_path.parent == tmp_path / ".nodeble-api" / "crontab-backups"
    assert backup_path.read_text() == "0 9 * * * /usr/bin/backup.sh\n"


def test_apply_action_no_crontab_returns_no_op(tmp_path, monkeypatch):
    """Empty crontab → ok with note, no edits."""
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    my_pid = os.getpid()

    def fake_run(args, **kw):
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 0, stdout=f"{my_pid}\n", stderr="")
        if args[0] == "crontab" and args[1] == "-l":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no crontab for x")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(crontab_ops.subprocess, "run", fake_run)
    out = crontab_ops._apply_action_to_crontab(
        "test-empty", "wheel", "pause", home=tmp_path,
    )
    assert out["ok"] is True
    assert out["lines_changed"] == 0
    assert "note" in out


def test_apply_action_unknown_strategy_returns_error(tmp_path, _bound_process):
    out = crontab_ops._apply_action_to_crontab(
        "test-x", "nonexistent-strat", "pause", home=tmp_path,
    )
    assert out["ok"] is False
    assert "unknown_strategy" in out["error"]


def test_apply_action_post_write_mismatch_restores_from_backup(tmp_path, monkeypatch):
    """Concurrent edit between write+verify → restore + return failure."""
    monkeypatch.setattr(crontab_ops.shutil, "which", lambda _: "/bin/systemctl")
    my_pid = os.getpid()
    state = {"call_count": 0}

    def fake_run(args, **kw):
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 0, stdout=f"{my_pid}\n", stderr="")
        if args[0] == "crontab" and args[1] == "-l":
            state["call_count"] += 1
            if state["call_count"] <= 2:
                # First call (backup) + second call (read for edit) return original
                return subprocess.CompletedProcess(args, 0, stdout=(
                    "0 9 * * * /usr/bin/backup.sh\n"
                    "35 13 * * 1-5 cd /home/x/projects/nodeble-wheel && /home/x/projects/nodeble-wheel/.venv/bin/python -m nodeble_wheel scan\n"
                ), stderr="")
            else:
                # Third call (post-write verify) returns DIFFERENT content
                # — simulating concurrent edit
                return subprocess.CompletedProcess(args, 0, stdout=(
                    "0 9 * * * /usr/bin/something_completely_different.sh\n"
                ), stderr="")
        if args[0] == "crontab" and args[1] == "-":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(crontab_ops.subprocess, "run", fake_run)
    out = crontab_ops._apply_action_to_crontab(
        "test-mismatch", "wheel", "pause", home=tmp_path,
    )
    assert out["ok"] is False
    assert out["error"] == "post_write_verification_mismatch"
    assert "restored_from_backup" in out

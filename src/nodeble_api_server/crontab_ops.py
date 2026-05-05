"""Safe crontab editing for api-server lifecycle endpoints (Path C Item 4).

Per L1 §7.11 #8 4-constraint contract — CEO 2026-05-05 ratify-with-CTO-
amendments (marker-based scoping replaced by path-substring + python -m
matching because verify-from-source confirmed marker comments are
inconsistent across the 9 strategy modules):

  (i)   Process binding — edit must run inside ``nodeble-api-server.service``
        systemd USER unit. We compare ``os.getpid()`` AND ``os.getppid()``
        against the unit's MainPID returned by ``systemctl --user show``.
        Either match passes (single-process uvicorn vs multi-worker setups
        differ in which one matches). Mismatch → refuse + diagnostic.

  (ii)  Path-substring + python -m matching (REPLACES marker scope) — a
        crontab line is in-scope for strategy ``X`` iff it contains BOTH
        ``/projects/<repo_dir>/`` and ``python -m <module_pkg>``. The
        trailing slash on the path prevents IC's ``/projects/nodeble/``
        from accidentally matching Wheel's ``/projects/nodeble-wheel/``.
        ``module_pkg`` is derived from the repo dir basename by replacing
        ``-`` with ``_`` (Tower verify-from-source 2026-05-05).

  (iii) Pre-edit backup — capture ``crontab -l`` to
        ``~/.nodeble-api/crontab-backups/<install_id>-<ts>.bak`` before any
        write. 30-day retention via best-effort cleanup on each call.

  (iv)  Post-edit verification — after ``crontab -`` write, re-read
        ``crontab -l`` and confirm the resulting content matches what we
        intended to write (out-of-scope lines unchanged, in-scope lines
        transformed exactly per action). Mismatch → restore from backup
        + return failure.

Public API for routes/server.py:

- ``pause_strategy(strategy, install_id)``  → comment-out in-scope lines with
  the ``# PAUSED-by-api: `` prefix so resume can find them.
- ``resume_strategy(strategy, install_id)`` → strip the prefix from in-scope
  lines.
- ``uninstall_strategy_cron(strategy, install_id)`` → remove in-scope lines
  entirely.

All three return a ``dict`` with ``ok: bool`` + diagnostic fields.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from nodeble_api_server.state_reader import STRATEGY_REGISTRY

logger = logging.getLogger(__name__)


# Default systemd user unit name for the api-server. Override via
# ``API_SERVER_SYSTEMD_UNIT`` env var on machines that name it differently.
DEFAULT_API_SERVER_UNIT = "nodeble-api-server.service"

# Marker prefixed onto crontab lines when paused. Symmetric — resume
# strips this exact prefix. Choosing a literal-comment leading char so
# cron treats the line as a comment (no execution).
PAUSE_MARKER = "# PAUSED-by-api: "

# Backup retention window — Constraint (iii).
BACKUP_RETENTION_DAYS = 30

# Subprocess timeouts — both `crontab` and `systemctl` are local + fast.
SUBPROCESS_TIMEOUT_SEC = 10


# ── Strategy → (repo_dir basename, module pkg) ──────────────────────────────


def module_pkg_for(strategy: str) -> str | None:
    """Derive the Python module package name from STRATEGY_REGISTRY repo_dir.

    Tower verify-from-source 2026-05-05: ``python -m <pkg>`` invocations
    use the repo basename with ``-`` replaced by ``_``:

    - ``nodeble-wheel`` → ``nodeble_wheel``
    - ``nodeble-directionalspread`` → ``nodeble_directionalspread``
    - ``nodeble`` (IC) → ``nodeble``  (single token, no transform)

    Returns ``None`` for unknown strategy.
    """
    meta = STRATEGY_REGISTRY.get(strategy)
    if not meta or "repo_dir" not in meta:
        return None
    basename = meta["repo_dir"].rsplit("/", 1)[-1]
    return basename.replace("-", "_")


def repo_dir_basename_for(strategy: str) -> str | None:
    meta = STRATEGY_REGISTRY.get(strategy)
    if not meta or "repo_dir" not in meta:
        return None
    return meta["repo_dir"].rsplit("/", 1)[-1]


# ── Constraint (i): process binding ─────────────────────────────────────────


def _systemd_main_pid(unit: str = DEFAULT_API_SERVER_UNIT) -> int | None:
    """Query ``systemctl --user show -p MainPID --value <unit>``.

    Returns the PID as an int, or ``None`` on:
      - systemctl missing (e.g. macOS dev box)
      - subprocess error / timeout
      - non-numeric output
      - MainPID == 0 (unit not running)
    """
    if shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("systemctl show MainPID failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid if pid > 0 else None


def check_process_binding(unit: str = DEFAULT_API_SERVER_UNIT) -> tuple[bool, str | None]:
    """Constraint (i). Returns ``(ok, diagnostic)``.

    Accepts either:
      - ``os.getpid() == MainPID``  (single-process uvicorn)
      - ``os.getppid() == MainPID`` (multi-worker uvicorn, master == MainPID)

    A non-systemd-managed run (e.g. dev: ``uvicorn app:app`` from shell)
    will not match — refuse the edit. This is the safety boundary the
    L1 §7.11 #8 carve-out depends on.
    """
    main_pid = _systemd_main_pid(unit)
    if main_pid is None:
        return False, (
            f"systemctl --user show -p MainPID {unit} returned no PID — "
            "either systemctl missing or unit not running"
        )
    my_pid = os.getpid()
    my_ppid = os.getppid()
    if my_pid == main_pid or my_ppid == main_pid:
        return True, None
    return False, (
        f"process not bound to {unit}: my pid={my_pid}, ppid={my_ppid}, "
        f"unit MainPID={main_pid}"
    )


# ── Constraint (ii): path-substring + python -m matching ────────────────────


def in_scope_crontab_line(line: str, repo_basename: str, module_pkg: str) -> bool:
    """Constraint (ii). True iff ``line`` contains BOTH:

    - ``/projects/<repo_basename>/`` (trailing slash forces full-token match
      so ``/projects/nodeble/`` does NOT also match
      ``/projects/nodeble-wheel/...``)
    - ``python -m <module_pkg>``

    Both substrings must be present. Comment markers don't gate anything
    (per CTO finding: marker conventions inconsistent across 9 modules).

    Note: a hand-written user line that happens to contain both substrings
    IS in-scope — the contract assumes users don't write competing lines
    that touch the same module's repo + python -m. Documented limitation.
    """
    has_path = f"/projects/{repo_basename}/" in line
    has_module = f"python -m {module_pkg}" in line
    return has_path and has_module


# ── Constraint (iii): pre-edit backup + retention ───────────────────────────


def _backup_dir(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".nodeble-api" / "crontab-backups"


def backup_crontab(install_id: str, home: Path | None = None) -> Path:
    """Constraint (iii). Capture ``crontab -l`` to a timestamped backup file.

    Returns the path to the backup. If ``crontab -l`` returns non-zero
    (no crontab installed yet), writes an empty file as the backup.
    """
    backup_dir = _backup_dir(home)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{install_id}-{ts}.bak"
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
        content = result.stdout if result.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        content = ""
    backup_path.write_text(content)
    return backup_path


def cleanup_old_backups(home: Path | None = None, days: int = BACKUP_RETENTION_DAYS) -> int:
    """Best-effort retention cleanup. Returns count of files removed."""
    backup_dir = _backup_dir(home)
    if not backup_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for f in backup_dir.glob("*.bak"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def restore_crontab_from_backup(backup_path: Path) -> bool:
    """Pipe backup contents into ``crontab -``. Returns True on success."""
    try:
        content = backup_path.read_text()
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["crontab", "-"], input=content, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


# ── Constraint (iv): post-edit verification ─────────────────────────────────


def verify_post_edit(
    before_lines: list[str],
    after_lines: list[str],
    in_scope_predicate: Callable[[str], bool],
    expected_action: str,  # "comment" | "uncomment" | "remove"
) -> tuple[bool, str | None]:
    """Constraint (iv). Verify only in-scope lines changed.

    Out-of-scope lines must be byte-identical before vs after. In-scope
    line counts must change per the action's contract:

    - ``comment`` / ``uncomment``: total line count unchanged (in-place
      transform of in-scope lines)
    - ``remove``: total line count = before - count(in-scope-before)

    Returns ``(ok, error_message)``. ``error_message`` is ``None`` on ok.
    """
    out_before = [l for l in before_lines if not in_scope_predicate(l)]
    out_after = [l for l in after_lines if not in_scope_predicate(l)]

    if out_before != out_after:
        return False, (
            f"out-of-scope lines changed (before {len(out_before)} != "
            f"after {len(out_after)}, or content differs)"
        )

    in_before_count = sum(1 for l in before_lines if in_scope_predicate(l))
    in_after_count = sum(1 for l in after_lines if in_scope_predicate(l))

    if expected_action in ("comment", "uncomment"):
        if len(after_lines) != len(before_lines):
            return False, (
                f"{expected_action} changed total line count "
                f"({len(before_lines)} → {len(after_lines)})"
            )
        if in_after_count != in_before_count:
            return False, (
                f"{expected_action} changed in-scope line count "
                f"({in_before_count} → {in_after_count})"
            )
    elif expected_action == "remove":
        expected_after_total = len(before_lines) - in_before_count
        if len(after_lines) != expected_after_total:
            return False, (
                f"remove changed total line count to {len(after_lines)} "
                f"(expected {expected_after_total})"
            )
        if in_after_count != 0:
            return False, f"remove left {in_after_count} in-scope lines"
    else:
        return False, f"unknown expected_action {expected_action!r}"

    return True, None


# ── Action transforms (apply per line) ──────────────────────────────────────


def _transform_pause(line: str) -> str:
    """Add PAUSE_MARKER prefix unless already paused or already commented."""
    if line.startswith(PAUSE_MARKER):
        return line  # already paused, idempotent no-op
    stripped = line.lstrip()
    if stripped.startswith("#"):
        # Already commented (manual?). Don't re-prefix; leave intact.
        return line
    return PAUSE_MARKER + line


def _transform_resume(line: str) -> str:
    """Strip PAUSE_MARKER prefix if present; else leave intact."""
    if line.startswith(PAUSE_MARKER):
        return line[len(PAUSE_MARKER):]
    return line


# ── High-level wrapper ─────────────────────────────────────────────────────


def _apply_action_to_crontab(
    install_id: str,
    strategy: str,
    action: str,  # "pause" | "resume" | "uninstall"
    home: Path | None = None,
    unit: str = DEFAULT_API_SERVER_UNIT,
) -> dict:
    """Apply ``action`` to crontab with all 4 constraints enforced.

    Return shape (consumed by routes/server.py)::

        {
          "ok": bool,
          "action": "pause" | "resume" | "uninstall",
          "lines_changed": int,
          "backup_path": str,
          "error"?: str (only when ok=False),
          "diagnostic"?: str (only on constraint-i refusal),
          "restored_from_backup"?: bool (only when restore happened),
        }
    """
    # Constraint (i): process binding
    bind_ok, bind_diag = check_process_binding(unit=unit)
    if not bind_ok:
        return {
            "ok": False,
            "error": "process_binding_check_failed",
            "diagnostic": bind_diag,
        }

    # Resolve strategy → repo basename + module pkg
    repo_basename = repo_dir_basename_for(strategy)
    module_pkg = module_pkg_for(strategy)
    if repo_basename is None or module_pkg is None:
        return {"ok": False, "error": f"unknown_strategy:{strategy}"}

    in_scope = lambda line: in_scope_crontab_line(line, repo_basename, module_pkg)

    # Constraint (iii): pre-edit backup + retention sweep
    backup_path = backup_crontab(install_id, home=home)
    cleanup_old_backups(home=home)  # best-effort

    # Read current crontab
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "ok": False, "error": f"crontab_read_failed:{exc}",
            "backup_path": str(backup_path),
        }

    # `crontab -l` returns rc=1 with stderr "no crontab for..." on empty
    # crontab. Treat as "no in-scope lines, no-op".
    if result.returncode != 0 and not result.stdout.strip():
        return {
            "ok": True, "action": action, "lines_changed": 0,
            "backup_path": str(backup_path),
            "note": "no crontab to edit",
        }

    before_lines = result.stdout.splitlines()

    # Apply action
    after_lines: list[str] = []
    lines_changed = 0
    for line in before_lines:
        if not in_scope(line):
            after_lines.append(line)
            continue
        if action == "pause":
            new = _transform_pause(line)
            if new != line:
                lines_changed += 1
            after_lines.append(new)
        elif action == "resume":
            new = _transform_resume(line)
            if new != line:
                lines_changed += 1
            after_lines.append(new)
        elif action == "uninstall":
            lines_changed += 1
            # skip — drops the line
        else:
            return {
                "ok": False, "error": f"unknown_action:{action}",
                "backup_path": str(backup_path),
            }

    # Constraint (iv): pre-write verification (cheap, before mutation)
    expected_action_map = {
        "pause": "comment", "resume": "uncomment", "uninstall": "remove",
    }
    ok, err = verify_post_edit(
        before_lines, after_lines, in_scope, expected_action_map[action],
    )
    if not ok:
        return {
            "ok": False,
            "error": f"pre_write_verification_failed:{err}",
            "backup_path": str(backup_path),
        }

    # Write new crontab
    new_content = "\n".join(after_lines) + ("\n" if after_lines else "")
    try:
        write_result = subprocess.run(
            ["crontab", "-"], input=new_content, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "ok": False, "error": f"crontab_write_failed:{exc}",
            "backup_path": str(backup_path),
        }
    if write_result.returncode != 0:
        return {
            "ok": False,
            "error": f"crontab_write_nonzero:{write_result.stderr[-200:].strip()}",
            "backup_path": str(backup_path),
        }

    # Constraint (iv) post-write: re-read and confirm content matches.
    try:
        verify_result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "ok": False, "error": f"crontab_post_read_failed:{exc}",
            "backup_path": str(backup_path),
        }
    if verify_result.returncode != 0:
        # Empty crontab is OK if uninstall removed everything.
        actual_after: list[str] = []
    else:
        actual_after = verify_result.stdout.splitlines()

    if actual_after != after_lines:
        # Concurrent edit OR write didn't take. Restore from backup.
        restored = restore_crontab_from_backup(backup_path)
        return {
            "ok": False,
            "error": "post_write_verification_mismatch",
            "backup_path": str(backup_path),
            "restored_from_backup": restored,
        }

    return {
        "ok": True,
        "action": action,
        "lines_changed": lines_changed,
        "backup_path": str(backup_path),
    }


def pause_strategy(strategy: str, install_id: str, home: Path | None = None) -> dict:
    """Comment-out all in-scope crontab lines for ``strategy``."""
    return _apply_action_to_crontab(install_id, strategy, "pause", home=home)


def resume_strategy(strategy: str, install_id: str, home: Path | None = None) -> dict:
    """Uncomment all paused-by-api crontab lines for ``strategy``."""
    return _apply_action_to_crontab(install_id, strategy, "resume", home=home)


def uninstall_strategy_cron(strategy: str, install_id: str, home: Path | None = None) -> dict:
    """Remove all in-scope crontab lines for ``strategy``."""
    return _apply_action_to_crontab(install_id, strategy, "uninstall", home=home)

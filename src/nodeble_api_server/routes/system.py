"""/api/v1/system/* — app-wide controls that fan out across strategies.

Currently hosts the Kill Switch: one button in the desktop app flips
`mode: dry_run` on every strategy's strategy.yaml so the next cron tick
runs simulated, without touching cron / systemd. The reverse (engage=false)
restores `mode: live`. Per-strategy best-effort: one failure doesn't roll
back the 8 others.

Why a dedicated endpoint instead of 9 /config writes from the client:
- Atomicity of intent: one audit entry at `strategy="system"` records the
  operator decision, plus per-strategy entries track mechanical progress.
- Avoids client-side orchestration (retries, partial failures) bleeding
  into UI state.
- The `mode` field is hidden from /config/editable-paths for the same
  reason — killswitch is the only write path, giving audit + UI a
  single source of truth.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from nodeble_api_server.audit import audit_path, write_event
from nodeble_api_server.audit_reader import read_audit_entries
from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.config_writer import run_shim
from nodeble_api_server.state_reader import (
    STRATEGY_REGISTRY,
    clear_cache,
    strategy_config_shim,
    strategy_venv_python,
)

router = APIRouter(
    prefix="/api/v1/system",
    dependencies=[Depends(require_bearer_token)],
)

_SERVER_TZ = ZoneInfo("America/New_York")

# Canonical audit labels — the frontend keys off these strings when
# reading /history/config back out, so they're stable contract.
_SYSTEM_STRATEGY_KEY = "system"
_SYSTEM_PARAM_PATH = "killswitch"


# ── Schemas ──────────────────────────────────────────────────────────────


class KillswitchPayload(BaseModel):
    engaged: bool
    # Reason is optional — in an emergency the operator should not be
    # blocked by a form. Max length capped so the field can't be weaponized
    # to bloat audit.jsonl.
    reason: str = Field(default="", max_length=500)


# ── Helpers ──────────────────────────────────────────────────────────────


def _read_mode(strategy_id: str, home: Path | None = None) -> str | None:
    """Parse `mode` directly from strategy.yaml, bypassing state_reader's
    5s TTL cache. The killswitch view must reflect ground truth — if
    someone changed mode via the shim CLI or by hand, we want to show
    that immediately, not a cached copy.
    """
    home = home or Path.home()
    meta = STRATEGY_REGISTRY.get(strategy_id)
    if not meta:
        return None
    path = home / meta["folder"] / "config" / "strategy.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return None
    mode = data.get("mode")
    return mode if isinstance(mode, str) else None


def _aggregate_state(per_strategy_mode: dict[str, str | None]) -> str:
    """Collapse per-strategy mode into one of four UI states:
    - 'engaged'    — ALL known-mode strategies are dry_run
    - 'disengaged' — ALL known-mode strategies are live
    - 'partial'    — mix
    - 'unknown'    — no strategies have a readable mode (fresh install?)
    """
    modes = [m for m in per_strategy_mode.values() if m is not None]
    if not modes:
        return "unknown"
    if all(m == "dry_run" for m in modes):
        return "engaged"
    if all(m == "live" for m in modes):
        return "disengaged"
    return "partial"


def _resolve_shim_for(
    strategy_id: str,
) -> tuple[str | None, Path | None, str | None]:
    """Return (shim_name, venv_python_path, error_msg). All-None tuple
    means strategy is registered but not shim-writable (no config_shim
    or missing venv). Used in the POST handler's per-strategy loop
    where we want to surface the error instead of 500-ing the batch."""
    if strategy_id not in STRATEGY_REGISTRY:
        return None, None, "unknown strategy"
    shim = strategy_config_shim(strategy_id)
    if not shim:
        return None, None, "no config_shim registered"
    venv = strategy_venv_python(strategy_id)
    if not venv or not venv.exists():
        return None, None, f"venv not found at {venv}"
    return shim, venv, None


def _latest_system_audit() -> dict | None:
    """Most recent `strategy=system param_path=killswitch` audit entry,
    or None. Used to surface engaged_at / last_change_reason on GET."""
    entries = read_audit_entries(
        path=audit_path(),
        strategy=_SYSTEM_STRATEGY_KEY,
        limit=50,
        before_ts=None,
    )
    for entry in entries:
        if entry.get("param_path") == _SYSTEM_PARAM_PATH:
            return entry
    return None


def _latest_operator_intent() -> str:
    """Last intent the operator committed via the killswitch endpoint.

    Returns "engaged" / "disengaged". Defaults to "disengaged" when
    there's no system/killswitch audit entry yet — fresh installs and
    the baseline state after-smoke-cleanup both present as "operator
    hasn't done anything with the killswitch yet".

    Reading intent from audit (not from per-strategy aggregate state)
    matters for UX: baseline installs have mixed per-strategy modes by
    design (Calendar / Collar / Straddle / Strangle / IronButterfly
    default to dry_run on fresh install). Keying engaged off the
    aggregate would flag the TopBar button as "partial / paused" from
    the moment the app first launches, which is wrong — the operator
    never pressed engage. engaged must reflect operator intent, state
    reflects ground truth, and the UI maps the (engaged, state) pair
    onto a 4-way button state.
    """
    latest = _latest_system_audit()
    if latest is None:
        return "disengaged"
    nv = latest.get("new_value")
    if nv in ("engaged", "disengaged"):
        return nv
    # Back-compat with pre-intent audit entries that stored aggregate
    # state in new_value (e.g. "partial"). Treat those as disengaged so
    # we don't leave an app permanently orange after a partial failure
    # in the pre-intent era.
    return "disengaged"


# ── Pre-engage snapshot (per-strategy mode restore on disengage) ────────


def _snapshot_path() -> Path:
    """Location of the pre-engage snapshot file.

    Resolved via Path.home() so tests that monkeypatch Path.home()
    land the file under tmp — no extra monkeypatching needed.
    """
    return Path.home() / ".nodeble-api" / "killswitch" / "pre_engage.json"


def _write_snapshot(
    per_strategy_mode: dict[str, str],
    captured_reason: str,
) -> bool:
    """Atomically persist the pre-engage per-strategy mode map.

    Called on the disengaged→engaged transition so a subsequent
    disengage can restore each strategy to EXACTLY its pre-engage
    mode — not to a blanket "live", which would flip the 5 baseline-
    dry_run strategies (Calendar / Collar / IronButterfly / Straddle
    / Strangle) into real trading against operator intent.

    Returns True on success, False on any I/O error. Emergency use
    case: a disk error must not block the operator's intent to flip
    everything to dry_run. Caller logs ERROR on False so the
    degraded-restore warning is visible.
    """
    path = _snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("killswitch snapshot: mkdir failed at %s: %s", path.parent, exc)
        return False

    payload_dict = {
        "captured_at": datetime.now(_SERVER_TZ).isoformat(),
        "captured_reason": captured_reason,
        "per_strategy_mode": per_strategy_mode,
    }
    # tempfile + os.replace = atomic. A crash mid-write can never leave
    # a half-written snapshot that would parse as valid JSON with
    # missing keys.
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload_dict, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.error("killswitch snapshot: write failed at %s: %s", path, exc)
        return False

    return True


def _read_snapshot() -> dict[str, str] | None:
    """Read the pre-engage snapshot's per_strategy_mode map, or None
    if the file is absent, malformed, or structurally wrong.

    Malformed snapshot is treated the same as missing — caller falls
    back to flip-all-to-live with a WARNING. The schema contract here
    is simple enough that we don't try to partial-recover; if JSON
    or key shape is off, don't trust any of it.
    """
    path = _snapshot_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("killswitch snapshot: read failed at %s: %s", path, exc)
        return None

    psm = data.get("per_strategy_mode") if isinstance(data, dict) else None
    if not isinstance(psm, dict):
        logger.warning(
            "killswitch snapshot: per_strategy_mode missing or wrong type "
            "in %s",
            path,
        )
        return None
    # Coerce to dict[str, str] — skip entries whose values aren't strings.
    return {
        str(k): v for k, v in psm.items() if isinstance(v, str)
    }


def _delete_snapshot() -> None:
    """Best-effort delete. Never raises — if we can't delete, the file
    just sticks around; next engage overwrites anyway."""
    path = _snapshot_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("killswitch snapshot: unlink failed at %s: %s", path, exc)


# ── GET: current state ──────────────────────────────────────────────────


@router.get("/killswitch")
def get_killswitch() -> dict:
    """Current killswitch state + per-strategy mode + operator history.

    Reads strategy.yaml directly (not via state_reader cache) so any
    out-of-band `mode` change surfaces immediately. Registered strategies
    whose yaml is missing or unparseable are reported as `mode=null`
    and excluded from the aggregate-state calculation.

    `engaged` reflects OPERATOR INTENT (last action through the
    killswitch endpoint), not the aggregate-state of the fleet. The UI
    pairs `engaged` + `state` to pick a 4-way button render:
        engaged=false                           → 🟢 running (baseline)
        engaged=true  & state="engaged"         → 🔴 paused (all flipped)
        engaged=true  & state="partial"         → 🟡 partial (some failed
                                                   OR someone changed a
                                                   strategy out-of-band)
        engaged=true  & state="unknown"         → ⚠️ anomaly
    """
    per_strategy = {sid: _read_mode(sid) for sid in STRATEGY_REGISTRY}
    state = _aggregate_state(per_strategy)
    intent = _latest_operator_intent()

    last = _latest_system_audit()
    engaged_at: str | None = None
    last_change_reason: str | None = None
    last_actor: str | None = None
    if last is not None and intent == "engaged":
        engaged_at = last.get("ts")
    if last is not None:
        last_change_reason = last.get("reason") or None
        last_actor = last.get("actor")

    return {
        "state": state,  # "engaged" | "disengaged" | "partial" | "unknown"
        "engaged": intent == "engaged",  # operator intent
        "engaged_at": engaged_at,
        "last_change_reason": last_change_reason,
        "last_actor": last_actor,
        "per_strategy_mode": per_strategy,
    }


# ── POST: flip state ────────────────────────────────────────────────────


@router.post("/killswitch")
def post_killswitch(payload: KillswitchPayload) -> dict:
    """Flip every registered strategy's `mode` to the target value.

    target = "dry_run" if payload.engaged else "live".

    Per-strategy best-effort: a shim failure on one strategy does NOT
    roll back the others. The response's `result` map tells the UI
    which strategies succeeded and the aggregate `state` tells it
    whether the whole fleet converged.

    Audit writes:
    - One per-strategy entry (strategy=<id>, param_path="mode") for
      every strategy the shim actually wrote (result="success" or
      "shim_error"). Strategies already at the target get no per-
      strategy entry — avoids audit spam on double-clicks.
    - One system-level entry (strategy="system", param_path="killswitch",
      old_value=<previous operator intent>, new_value=<new intent>)
      recording the operator decision. new_value is "engaged" or
      "disengaged" — not the aggregate state — so GET can derive the
      `engaged` flag purely from the latest audit entry without
      consulting per-strategy modes.

    Pre-engage snapshot lifecycle (safety for the 5 baseline-dry_run
    strategies — Calendar / Collar / IronButterfly / Straddle / Strangle):
    - On disengaged→engaged transition, capture each strategy's current
      mode to a snapshot file BEFORE flipping anything.
    - On disengaged target, read the snapshot and restore each strategy
      to its snapshot value (not a blanket "live").
    - Missing / corrupt snapshot → fall back to flip-all-to-live with a
      WARNING log. Preserves pre-snapshot behavior.
    - Delete snapshot only on full disengage success. Partial failure
      keeps it so the operator's retry uses the same baseline.
    """
    target_intent = "engaged" if payload.engaged else "disengaged"

    # Snapshot the starting state: pre_state for UI "summary" text,
    # pre_intent for the system audit's old_value + the snapshot-write
    # trigger (only write on disengaged→engaged transitions).
    pre_modes = {sid: _read_mode(sid) for sid in STRATEGY_REGISTRY}
    pre_state = _aggregate_state(pre_modes)
    pre_intent = _latest_operator_intent()

    reason = payload.reason or ""

    # ── Decide the per-strategy target mode ──────────────────────────
    # Engage: everyone to dry_run.
    # Disengage: restore each strategy to its pre-engage snapshot mode,
    #   or "live" as fallback when snapshot missing / corrupt / doesn't
    #   know about this strategy (edge: strategy added post-engage).
    target_per_strategy: dict[str, str] = {}
    if payload.engaged:
        for sid in STRATEGY_REGISTRY:
            target_per_strategy[sid] = "dry_run"
        # Write snapshot only on a real disengaged→engaged transition.
        # Re-engage while already engaged preserves the original
        # snapshot (oldest baseline wins).
        if pre_intent == "disengaged":
            snap_modes = {
                sid: m for sid, m in pre_modes.items() if isinstance(m, str)
            }
            captured_reason = reason or "<no reason given>"
            if not _write_snapshot(snap_modes, captured_reason):
                # Write failed — log already emitted. Engage continues
                # because operator intent is emergency-class (flip
                # everything to dry_run). Degraded disengage (will
                # fall back to all-live) is the tradeoff.
                logger.error(
                    "killswitch engage proceeding without snapshot — "
                    "subsequent disengage will flip all to live"
                )
    else:
        snapshot = _read_snapshot()
        if snapshot is None:
            # No snapshot + operator pressing disengage. Two cases:
            # (a) never engaged through this endpoint → restore = all-live
            # (b) snapshot was rm'd / corrupt → degraded restore.
            # Both → fall back to flip-all-to-live (old pre-snapshot
            # behavior). Warn so we notice if (b) starts happening in
            # production.
            logger.warning(
                "killswitch disengage: no pre-engage snapshot — falling "
                "back to flip-all-to-live. 5 baseline-dry_run strategies "
                "(if any) may be flipped against operator intent."
            )
            for sid in STRATEGY_REGISTRY:
                target_per_strategy[sid] = "live"
        else:
            for sid in STRATEGY_REGISTRY:
                # Strategies absent from the snapshot (added between
                # engage and disengage) default to "live" — same as
                # the pre-snapshot behavior for unknown strategies.
                target_per_strategy[sid] = snapshot.get(sid, "live")

    # ── Per-strategy flip loop ───────────────────────────────────────
    result: dict[str, dict[str, Any]] = {}
    for sid, meta in STRATEGY_REGISTRY.items():
        current = pre_modes.get(sid)
        target_mode = target_per_strategy[sid]

        # Build reason per-strategy because the target now varies.
        per_strategy_reason = (
            f"system.killswitch -> {target_mode}"
            + (f" ({reason})" if reason else "")
        )

        # No-op path — skip the shim call + don't pollute audit.jsonl.
        if current == target_mode:
            result[sid] = {
                "ok": True,
                "old_mode": current,
                "new_mode": current,
                "changed": False,
            }
            continue

        shim_name, venv_python, err = _resolve_shim_for(sid)
        if err is not None:
            result[sid] = {
                "ok": False,
                "old_mode": current,
                "new_mode": None,
                "changed": False,
                "error": err,
            }
            write_event(
                strategy=sid,
                param_path="mode",
                old_value=current,
                new_value=target_mode,
                reason=per_strategy_reason,
                result="shim_error",
                error=err,
            )
            continue

        shim_result = run_shim(
            venv_python=str(venv_python),
            shim_name=shim_name,  # type: ignore[arg-type]
            action="set",
            strategy_id=sid,
            param_path="mode",
            value=target_mode,
        )
        if shim_result.ok:
            result[sid] = {
                "ok": True,
                "old_mode": shim_result.old,
                "new_mode": shim_result.new,
                "changed": True,
            }
            write_event(
                strategy=sid,
                param_path="mode",
                old_value=shim_result.old,
                new_value=shim_result.new,
                reason=per_strategy_reason,
                result="success",
                error=None,
            )
        else:
            err_text = shim_result.error or "shim set failed"
            category = (
                "timeout" if "timed out" in err_text.lower() else "write_failed"
            )
            result[sid] = {
                "ok": False,
                "old_mode": current,
                "new_mode": None,
                "changed": False,
                "error": err_text,
            }
            write_event(
                strategy=sid,
                param_path="mode",
                old_value=current,
                new_value=target_mode,
                reason=per_strategy_reason,
                result=category,
                error=err_text,
            )

    # Invalidate the state_reader cache so the very next /strategies
    # request reflects the new mode field instead of serving the 5-s
    # cached copy (mirrors what commit_config does for config edits).
    clear_cache()

    # ── Snapshot cleanup on DISENGAGE ────────────────────────────────
    # Delete only on full success — partial disengage leaves the
    # snapshot so operator retry runs against the same baseline.
    if not payload.engaged:
        all_ok = all(r["ok"] for r in result.values())
        if all_ok:
            _delete_snapshot()

    # Re-read to compute the post aggregate state.
    post_modes = {sid: _read_mode(sid) for sid in STRATEGY_REGISTRY}
    post_state = _aggregate_state(post_modes)

    # System-level audit: records the operator's intent + the observed
    # aggregate transition. Always written, even on no-op (a deliberate
    # press is still auditable — but it's clearly `result=noop`).
    ok_count = sum(1 for r in result.values() if r["ok"])
    total = len(result)
    changed_count = sum(1 for r in result.values() if r.get("changed"))
    if changed_count == 0:
        sys_result = "noop"
    elif ok_count == total:
        sys_result = "success"
    else:
        sys_result = "partial"

    write_event(
        strategy=_SYSTEM_STRATEGY_KEY,
        param_path=_SYSTEM_PARAM_PATH,
        old_value=pre_intent,
        new_value=target_intent,
        reason=reason,
        result=sys_result,
        error=None,
    )

    now_iso = datetime.now(_SERVER_TZ).isoformat()
    return {
        # engaged reflects OPERATOR INTENT (what they just requested),
        # not the aggregate state. State handles ground truth; the UI
        # uses (engaged, state) together to pick a 4-way button render.
        "engaged": payload.engaged,
        "state": post_state,
        "engaged_at": now_iso if payload.engaged else None,
        "result": result,
        "summary": f"{ok_count}/{total} 策略成功切换 · 状态 {post_state}",
    }


def _raise_if_locked() -> None:
    """Placeholder for future: if M4 adds a "frozen" per-installation
    setting, the killswitch endpoints should refuse to change state
    until the operator unlocks. Left as a no-op now so we don't forget
    to wire it up later."""
    return


# HTTPException import kept for future extensions even if unused today.
_ = HTTPException

"""``/api/v1/orchestrator/*`` — read & control surface for the allocator.

Phase O.A redesign (per ``~/projects/ceo/plans/2026-04-27-orchestrator-
redesign-spec.md``, amended 2026-05-04 §1.5 distribution channels):

5 endpoints total — 1 pre-existing (GET /allocation), 4 new:

- ``GET /allocation`` — pre-existing, return current allocation.json as-is
- ``POST /allocate`` — subprocess to orchestrator CLI, idempotency 409
- ``GET /installed-strategies`` — subprocess to ``detect-installed`` CLI
- ``GET /overrides`` — read overrides.yaml as JSON
- ``PUT /overrides`` — validate (``cap_step_violation`` 422) + atomic write

Q1 lock (协作总监 + CEO 5/4): routes live here in api-server; integration
with orchestrator is **file-based + subprocess**, no Python code import.
This keeps api-server independently shippable (orchestrator updates don't
break api-server install) and ensures cron + HTTP both use the exact same
CLI binary as their execution path. Side effect: a small amount of read-
side logic (lock age, overrides yaml load) is duplicated here mirroring
``nodeble_orchestrator.idempotency`` / ``nodeble_orchestrator.overrides``
— acceptable since these are simple file shapes and the writer (CLI +
allocator) remains the single source of truth.

Per L1 §1.5 (5/4 amendment): this layer is Mac-app-only. Git-clone
single-bot users (e.g. YB) don't run api-server, so these endpoints
never fire for them.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nodeble_api_server import state_reader
from nodeble_api_server.auth import require_bearer_token
from nodeble_api_server.state_reader import read_allocation, STRATEGY_REGISTRY

logger = logging.getLogger(__name__)


# ── Constants (mirror orchestrator-side) ────────────────────────────────────

# Step constraint matches orchestrator/overrides.CAP_STEP_USD (协作总监 5/4 lock).
CAP_STEP_USD = 10

# Default idempotency window (seconds) for 409 hint. Matches orchestrator's
# idempotency.DEFAULT_WINDOW_SEC. 协作总监 5/4 default; "实测调".
DEFAULT_IDEMPOTENCY_WINDOW_SEC = 60

# Subprocess timeout for ``allocate`` — full pipeline takes ~3s on Tower
# but Tiger API hiccup could stretch it. Cap at 120s so a frozen subprocess
# can't tie up the FastAPI worker indefinitely.
ALLOCATE_SUBPROCESS_TIMEOUT_SEC = 120
DETECT_SUBPROCESS_TIMEOUT_SEC = 30


# ── Path resolution (lazy, ``Path.home()`` per-call so test monkeypatch works) ─


def _orchestrator_python(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "projects" / "nodeble-orchestrator" / ".venv" / "bin" / "python"


def _allocate_lock_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".nodeble-orchestrator" / "data" / ".allocate.lock"


def _overrides_yaml_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".nodeble-orchestrator" / "config" / "overrides.yaml"


def _total_pool_path(home: Path | None = None) -> Path:
    # Orchestrator's own config dir — api-server already writes
    # overrides.yaml here (established pattern). Orchestrator's
    # `capital_pool.load_total_pool` reads exactly this path/schema
    # (contract ~/projects/cto/reviews/2026-05-16-total-pool-api-contract.md §2).
    base = home or Path.home()
    return base / ".nodeble-orchestrator" / "config" / "total_pool.json"


# ── total-pool validity bounds (T-20260516-105451 #3) ──────────────────────
#
# SINGLE SOURCE OF TRUTH = nodeble_orchestrator/capital_pool.py
# (MIN_REASONABLE_POOL_USD / MAX_REASONABLE_POOL_USD). Per contract §4:
# api-server may run in a SEPARATE venv from nodeble_orchestrator on
# some deploys (single-bot / fresh VM) so we cannot hard-import it.
# These constants are REPLICATED with this comment + guarded by
# `test_total_pool_bounds_match_orchestrator_contract` (contract-drift
# test) so a value the app accepts can never be silently rejected by
# the cron later. If you change these, change the orchestrator reader
# + the contract doc §4 in the same PR.
MIN_REASONABLE_POOL_USD = 1_000.0
MAX_REASONABLE_POOL_USD = 1_000_000_000.0  # $1B


def _is_valid_pool_usd(v: object) -> bool:
    """True iff `v` is a real number (NOT bool) within [MIN, MAX].
    Mirrors the orchestrator reader's reject set (contract §4)."""
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        return False
    if not isinstance(v, (int, float)):
        return False
    if v <= 0:
        return False
    return MIN_REASONABLE_POOL_USD <= float(v) <= MAX_REASONABLE_POOL_USD


def _read_total_pool(home: Path | None = None) -> dict | None:
    """Parse total_pool.json. Returns the dict iff present + the
    `total_pool_usd` is valid per §4 bounds; else None (absent /
    corrupt / out-of-bounds all collapse to "not declared" — the gate
    + the orchestrator reader must agree on validity)."""
    p = _total_pool_path(home)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not _is_valid_pool_usd(
        data.get("total_pool_usd")
    ):
        return None
    return data


def _write_total_pool_json(
    total_pool_usd: float,
    source: str,
    home: Path | None = None,
) -> str:
    """Atomic write (tmp + os.replace, mode 0600 — config-dir hygiene
    matching overrides.yaml). Returns the ISO-8601 `updated_at` written.
    Contract §3.2 step 2."""
    p = _total_pool_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    updated_at = datetime.now(timezone.utc).astimezone().isoformat()
    payload = {
        "total_pool_usd": total_pool_usd,
        "updated_at": updated_at,
        "source": source,
    }
    fd, tmp_path = tempfile.mkstemp(
        dir=str(p.parent), prefix=".total_pool_", suffix=".json.tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return updated_at


# ── Pydantic request models ────────────────────────────────────────────────


class OverrideCap(BaseModel):
    """Per-strategy user override.

    ``fixed_cap_usd`` validation is intentionally strict:

    - Must be ``int`` (Pydantic strict; floats will be coerced if possible
      then re-validated — ``50.0`` → ``50`` passes, ``50.5`` fails)
    - Must be ``>= 0`` (``$0`` = "disable strategy" per UX §3.3, valid)
    - Must be a multiple of :data:`CAP_STEP_USD` ($10) — frontend slider
      step is $100 default + Shift-modifier $10 (前端总监 5/4 decision)

    Validation error for step uses literal ``cap_step_violation`` token
    so frontend can grep — 协作总监 5/4 PUT contract.

    ``extra='forbid'`` (lesson #52 from frontend audit 5/4) — silently
    accepting unknown fields turned the missing-envelope curl into a
    "PUT 200 no-op" trap. Strict rejection surfaces shape bugs at 422.
    """

    model_config = ConfigDict(extra="forbid")

    fixed_cap_usd: int = Field(..., ge=0)
    locked: bool

    @field_validator("fixed_cap_usd")
    @classmethod
    def must_be_step_multiple(cls, v: int) -> int:
        if v % CAP_STEP_USD != 0:
            raise ValueError(f"cap_step_violation: not multiple of ${CAP_STEP_USD}")
        return v


class OverridesIn(BaseModel):
    """Full snapshot of user overrides — replace semantics.

    PUT body shape mirrors ``overrides.yaml`` top-level structure so the
    file-on-disk and the wire are isomorphic. Strategies omitted from
    ``overrides`` are **cleared** (allocator falls back to computed cap).

    ``extra='forbid'`` rejects bodies missing the ``overrides:`` envelope
    (e.g. the curl ``{ic: {...}}`` shape that frontend audit 5/4
    misdiagnosed as a backend bug — lesson #52).
    """

    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, OverrideCap] = Field(default_factory=dict)


class AllocateIn(BaseModel):
    """POST /allocate body.

    ``extra='forbid'`` — typo'd flags should 422, not silently no-op
    (lesson #52 from frontend audit 5/4).
    """

    model_config = ConfigDict(extra="forbid")

    respect_overrides: bool = False
    force_nlv_refresh: bool = False
    # Bypass idempotency lock — frontend asks user "cron just ran <Xs ago,
    # force?" and re-POSTs with force=True on confirm.
    force: bool = False


class TotalPoolIn(BaseModel):
    """POST /total-pool body (contract §3.2). `extra='forbid'` — typo'd
    fields 422 not silent no-op (same rationale as AllocateIn).

    Bounds validation is intentionally NOT a pydantic `Field(ge=…)` —
    the contract wants a plain-language 422 message the app surfaces
    verbatim (Yongtao Layman), so the handler validates + raises with
    custom copy rather than pydantic's generic "ensure this value is
    greater than or equal to …".
    """

    model_config = ConfigDict(extra="forbid")

    # `Any` (NOT `float`) on purpose: pydantic's lax mode coerces JSON
    # `true`→1.0 / `"250000"`→250000.0 BEFORE the handler, which would
    # (a) let a bool slip past the contract §4 "reject bool" rule and
    # (b) replace our plain-language 422 with pydantic's generic error
    # list — but contract §3.2 requires `detail` be the verbatim
    # plain-language string the app surfaces. So we accept the raw
    # value untouched and validate entirely in-handler via
    # `_is_valid_pool_usd` (which sees the true runtime type).
    total_pool_usd: Any
    # "user_declared" (MVP Step 6.5 first set) vs "settings_edit"
    # (later Settings edit). Optional — handler infers from
    # file-already-existed when absent (contract §3.2 step 2).
    source: str | None = None


# ── Lock helpers (read-side only — orchestrator owns the writer) ──────────


def _read_lock_timestamp(home: Path | None = None) -> datetime | None:
    """Mirrors ``nodeble_orchestrator.idempotency.read_lock``. Inlined here
    to honor Q1's no-code-import constraint."""
    p = _allocate_lock_path(home)
    if not p.exists():
        return None
    try:
        return datetime.fromisoformat(p.read_text().strip())
    except (OSError, ValueError) as exc:
        logger.warning("allocate lock unreadable (%s)", exc)
        return None


def _is_locked(
    home: Path | None = None,
    window_sec: int = DEFAULT_IDEMPOTENCY_WINDOW_SEC,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """Return ``(locked, lock_ts)``. ``locked`` true iff lock exists and is
    younger than window. ``lock_ts`` returned for diagnostic in 409."""
    ts = _read_lock_timestamp(home)
    if ts is None:
        return False, None
    n = now or datetime.now(timezone.utc)
    age = (n - ts).total_seconds()
    return age < window_sec, ts


# ── Overrides yaml helpers (read-side + atomic write) ─────────────────────


def _read_overrides_yaml(home: Path | None = None) -> dict[str, dict]:
    """Return ``{strategy: {fixed_cap_usd, locked}}``. Empty dict if file
    missing / malformed / has no ``overrides:`` key. Distinct from
    orchestrator's read which differentiates None vs {} — api-server
    flattens since the HTTP response shape is always
    ``{"overrides": {...}}``."""
    p = _overrides_yaml_path(home)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text())
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("overrides.yaml unreadable (%s)", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    overrides = raw.get("overrides", {})
    return overrides if isinstance(overrides, dict) else {}


def _write_overrides_yaml(
    overrides_map: dict[str, dict],
    home: Path | None = None,
) -> None:
    """Atomic write — temp file in same dir + ``os.replace``. Mirrors
    orchestrator-side ``overrides.save_overrides``. The file shape is
    intentionally identical so allocator (orchestrator) reads it
    transparently."""
    p = _overrides_yaml_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "overrides": overrides_map,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "api_server_put",
    }

    fd, tmp_path = tempfile.mkstemp(
        dir=str(p.parent), prefix=".overrides_", suffix=".yaml.tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Sum-check (informational; allocator does the authoritative one too) ───


def _compute_sum_check(
    overrides_map: dict[str, dict],
    home: Path | None = None,
) -> dict[str, Any]:
    """Read current allocation.json for NLV + cash_reserved, compute
    ``Σ user-set caps + Σ remaining computed caps + cash_reserved`` vs
    NLV. Used by PUT /overrides response so frontend can show "your
    settings would over-allocate by $X" warning.

    No allocation.json yet (first install) → returns ``{"ok": null,
    "reason": "no_baseline"}`` (frontend treats as "ok unless we hear
    otherwise from the next allocate run").
    """
    alloc = read_allocation(home=home)
    if alloc is None:
        return {"ok": None, "reason": "no_baseline_allocation"}

    nlv = alloc.get("portfolio_nlv") or 0
    cash_reserved = alloc.get("cash_reserved") or 0
    strategies = alloc.get("strategies") or {}

    # Apply user overrides on top of computed caps to simulate post-allocate state.
    sum_caps = 0
    for strat, info in strategies.items():
        if not isinstance(info, dict):
            continue
        ovr = overrides_map.get(strat)
        if ovr and isinstance(ovr.get("fixed_cap_usd"), int):
            sum_caps += int(ovr["fixed_cap_usd"])
        else:
            sum_caps += info.get("max_buying_power") or 0

    headroom = nlv - sum_caps - cash_reserved
    return {
        "ok": headroom >= 0,
        "sum_caps_usd": sum_caps,
        "cash_reserved_usd": round(cash_reserved),
        "portfolio_nlv": nlv,
        "headroom_usd": round(headroom),
    }


# ── Subprocess wrappers ───────────────────────────────────────────────────


def _run_allocate_subprocess(
    payload: AllocateIn,
    idempotency_window: int = DEFAULT_IDEMPOTENCY_WINDOW_SEC,
    home: Path | None = None,
) -> tuple[int, str, str]:
    """Invoke ``python -m nodeble_orchestrator allocate [...]``.

    Returns ``(returncode, stdout, stderr)``. Caller handles non-zero
    return code → 5xx mapping.
    """
    python_path = _orchestrator_python(home)
    args = [
        str(python_path), "-m", "nodeble_orchestrator", "allocate",
        f"--idempotency-window={idempotency_window}",
    ]
    if payload.respect_overrides:
        args.append("--respect-overrides")
    if payload.force_nlv_refresh:
        args.append("--force-nlv-refresh")

    logger.info("Subprocess allocate: %s", " ".join(args))
    result = subprocess.run(
        args, capture_output=True, text=True,
        timeout=ALLOCATE_SUBPROCESS_TIMEOUT_SEC,
    )
    return result.returncode, result.stdout, result.stderr


def _run_detect_installed_subprocess(
    home: Path | None = None,
) -> dict[str, dict]:
    """Invoke ``python -m nodeble_orchestrator detect-installed`` and
    parse its JSON stdout.

    On any failure (subprocess error, non-JSON output, missing python)
    raises ``HTTPException(500)`` with a diagnostic detail. The caller
    can catch it or let FastAPI propagate.
    """
    python_path = _orchestrator_python(home)
    try:
        result = subprocess.run(
            [str(python_path), "-m", "nodeble_orchestrator", "detect-installed"],
            capture_output=True, text=True,
            timeout=DETECT_SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"detect-installed subprocess failed: {exc}",
        )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"detect-installed exited {result.returncode}: "
                   f"{result.stderr[-500:].strip()}",
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"detect-installed produced non-JSON: {exc}",
        )


# ── Router ────────────────────────────────────────────────────────────────


router = APIRouter(
    prefix="/api/v1/orchestrator",
    dependencies=[Depends(require_bearer_token)],
)


@router.get("/allocation")
def get_allocation() -> dict:
    """Return ~/.nodeble-orchestrator/data/allocation.json as-is.

    The schema is whatever the orchestrator writes — api-server doesn't
    re-shape or validate. Consumers treat it as opaque JSON with known
    top-level fields (regime / composite_score / portfolio_nlv /
    strategies{} / account_profile / generated_at). Missing file → 404
    so the UI can show a "orchestrator 还未跑过" empty state instead of
    a confused partial render.
    """
    data = read_allocation()
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="allocation.json not found — run orchestrator first",
        )
    return data


@router.post("/allocate")
def post_allocate(payload: AllocateIn) -> dict:
    """Trigger a fresh allocate run (manual ``分配检查`` button).

    Idempotency: if a previous allocate completed within the last 60s
    (default) and ``force=False``, returns ``409 Conflict`` with the
    previous lock timestamp so the frontend can ask the user to confirm
    re-run. ``force=True`` bypasses the check.

    Subprocess: invokes the orchestrator CLI with the requested flags.
    Cron and HTTP both go through the same CLI path — single source of
    truth for the allocate pipeline.

    Returns the freshly-written allocation.json + ``idempotency_lock_ts``
    for the UI's "last run at" display.
    """
    locked, lock_ts = _is_locked()
    if locked and not payload.force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "allocate_recently_run",
                "lock_ts": lock_ts.isoformat() if lock_ts else None,
                "lock_until": (lock_ts + timedelta(seconds=DEFAULT_IDEMPOTENCY_WINDOW_SEC)).isoformat() if lock_ts else None,
                "hint": "POST again with force=true to bypass",
            },
        )

    try:
        rc, stdout, stderr = _run_allocate_subprocess(payload)
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"allocate subprocess timed out (>{ALLOCATE_SUBPROCESS_TIMEOUT_SEC}s)",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"allocate subprocess failed to start: {exc}",
        )

    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "allocate_subprocess_nonzero",
                "exit_code": rc,
                "stderr_tail": stderr[-1000:].strip(),
            },
        )

    # Subprocess wrote allocation.json + lock. Read fresh to return.
    #
    # P2 cache-staleness fix (CTO 5/4 verdict file
    # cto/reviews/2026-05-04-orch-phase-oa-post-verify.md, frontend
    # Step 4 audit reproduce): state_reader has a 5s TTL cache used by
    # GET /allocation. Without explicit invalidation, this read after
    # subprocess write would return the pre-write cached value — POST
    # /allocate would respond with the stale allocation, and any
    # immediate GET /allocation would also be stale until TTL expires.
    # Clear the cache so both this response AND subsequent GETs see the
    # fresh file.
    state_reader.clear_cache()
    fresh = read_allocation()
    if fresh is None:
        # Subprocess reported success but file isn't there — file race or bug.
        raise HTTPException(
            status_code=500,
            detail="allocate completed but allocation.json missing post-write",
        )

    return fresh


@router.get("/installed-strategies")
def get_installed_strategies() -> dict[str, dict]:
    """Return ``{strategy: {installed, has_venv, service_active}}`` for all 9
    strategies.

    Subprocess to orchestrator's ``detect-installed`` CLI — single source
    of truth for the strategy ↔ repo ↔ service mapping (lives in
    ``nodeble_orchestrator.installed_detector.STRATEGY_REPO_REGISTRY``).
    """
    return _run_detect_installed_subprocess()


@router.get("/overrides")
def get_overrides() -> dict:
    """Return current contents of ``~/.nodeble-orchestrator/config/overrides.yaml``.

    Always returns ``{"overrides": {...}}`` shape — empty dict if no file
    or no overrides set, distinct from 404 since the file is optional
    (allocator treats absent identically to ``overrides: {}``).
    """
    return {"overrides": _read_overrides_yaml()}


@router.put("/overrides")
def put_overrides(payload: OverridesIn) -> dict:
    """Replace overrides.yaml with ``payload.overrides`` (full snapshot).

    Validation:
    - Pydantic field validators on ``OverrideCap`` reject step violations
      and negative values with 422 before we ever reach this body
    - Strategy keys are not validated against the 9-strategy whitelist
      here — orchestrator's allocator silently ignores unknown keys, and
      we want PUT to be permissive so the frontend can freely add /
      remove without race-tracking the install-state catalogue

    Sum-check: informational only. Allocator does its own
    authoritative sum-check at allocate-time and writes
    ``sum_caps_violation`` to ``allocation.json[warnings]`` if exceeded.
    PUT response includes the sum-check so the frontend can warn
    immediately on save (per 协作总监 5/4 example).

    Returns ``{"applied": True, "sum_check_result": {...}}``.
    """
    # Convert Pydantic models back to plain dicts for yaml serialization.
    overrides_dict: dict[str, dict] = {
        strat: cap.model_dump()
        for strat, cap in payload.overrides.items()
    }

    _write_overrides_yaml(overrides_dict)

    sum_check = _compute_sum_check(overrides_dict)

    return {"applied": True, "sum_check_result": sum_check}


# ── total-pool (T-20260516-105451 #3 — capital-input-upfront) ──────────────
# Contract: ~/projects/cto/reviews/2026-05-16-total-pool-api-contract.md
# Orchestrator owns the schema (capital_pool.load_total_pool); api-server
# writes the file + triggers re-allocate. NOT a CTO spec (data-handoff
# between two server-side components, like overrides.yaml).


@router.get("/total-pool")
def get_total_pool() -> dict:
    """Reliability-gate query (contract §3.1). The app BLOCKS main UI
    while ``declared == false``. 200 ALWAYS — ``declared:false`` is a
    normal state (fresh box never told its pool / corrupt / out of
    bounds), not an error. Validity uses the SAME bounds as the
    orchestrator reader (§4) so the gate + cron agree."""
    data = _read_total_pool()
    if data is None:
        return {"declared": False, "total_pool_usd": None, "updated_at": None}
    return {
        "declared": True,
        "total_pool_usd": data["total_pool_usd"],
        "updated_at": data.get("updated_at"),
    }


@router.post("/total-pool")
def post_total_pool(payload: TotalPoolIn) -> dict:
    """Set the declared total pool (contract §3.2).

    1. Validate bounds → 422 w/ plain-language message (app surfaces
       verbatim — Yongtao Layman).
    2. Atomic write total_pool.json (0600).
    3. Trigger re-allocate via the EXISTING allocate codepath
       (``_run_allocate_subprocess``) so the new pool takes effect now
       instead of waiting for the 09:58 ET cron. Reuses the existing
       allocate lock (the orchestrator CLI's ``--idempotency-window``)
       — NO second lock mechanism. Subprocess error / timeout / non-zero
       (e.g. scores not yet present on a brand-fresh box) → still 200
       for the SET, ``reallocate:"deferred"`` (next cron picks it up);
       UI is never blocked on the re-allocate.
    """
    v = payload.total_pool_usd
    if not _is_valid_pool_usd(v):
        # Plain-language, app-surfaced verbatim. Distinguish the two
        # common operator mistakes (too small = entered cents/units;
        # too large = typo) per Yongtao Layman.
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            msg = "请输入一个有效的美元金额 (大于 0 的数字)"
        elif float(v) < MIN_REASONABLE_POOL_USD:
            msg = (
                f"金额太小 (最低 ${int(MIN_REASONABLE_POOL_USD):,}) — "
                "请确认你输入的是美元不是分"
            )
        else:
            msg = (
                f"金额太大 (上限 ${int(MAX_REASONABLE_POOL_USD):,}) — "
                "请确认没有多打几个 0"
            )
        raise HTTPException(status_code=422, detail=msg)

    # source: explicit flag wins; else infer (file existed → a later
    # Settings edit; absent → first MVP Step 6.5 declaration).
    if payload.source in ("user_declared", "settings_edit"):
        source = payload.source
    else:
        source = (
            "settings_edit"
            if _total_pool_path().exists()
            else "user_declared"
        )

    updated_at = _write_total_pool_json(float(v), source)

    # Re-allocate (best-effort — never block the set on it). Reuse the
    # existing subprocess + lock; respect_overrides mirrors the cron /
    # AllocationCheckButton default.
    reallocate = "deferred"
    try:
        rc, _stdout, _stderr = _run_allocate_subprocess(
            AllocateIn(respect_overrides=True)
        )
        if rc == 0:
            reallocate = "ok"
            # Same cache-staleness fix as POST /allocate — clear so a
            # subsequent GET /allocation sees the fresh pool-based run.
            state_reader.clear_cache()
    except (subprocess.SubprocessError, OSError, subprocess.TimeoutExpired):
        # Brand-fresh box (scores absent) etc. — set succeeded, defer
        # the allocate to the next cron tick. NOT a 5xx.
        reallocate = "deferred"

    return {
        "status": "ok",
        "total_pool_usd": float(v),
        "updated_at": updated_at,
        "reallocate": reallocate,
    }

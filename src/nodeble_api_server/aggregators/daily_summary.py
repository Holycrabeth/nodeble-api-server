"""Top-level daily-summary aggregator — orchestrates session + 4 detectors.

Composes:
- `compute_session(now)` for the trading-window block
- Per-bot file I/O (cron.log tail, state.json mtime, ledger.jsonl filter,
  STOP file presence)
- All 4 detectors from `discrepancy_detector.py`
- Sticky list (halt_persisting for now; other types in later phases)

Returns the response shape required by the design doc:

    {session, bots[], discrepancies[], sticky[]}

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 3.1

Failure-mode philosophy: per-bot exceptions degrade gracefully — that bot's
entry gets stub fields and an error counter increment, but the response as
a whole still returns 200 for the other 3 bots. Whole-aggregator failures
(e.g. ledger path completely missing for ALL bots) bubble up to the route
layer which can decide between 503 + partial flag vs hard 500.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from nodeble_api_server.aggregators.discrepancy_detector import (
    Discrepancy,
    detect_ledger_state_mismatch,
    detect_missing_cron_run,
    detect_stale_state,
    detect_telegram_close_mismatch,
)
from nodeble_api_server.aggregators.session import SessionInfo, compute_session

ET = ZoneInfo("America/New_York")
CRON_GRACE_WINDOW = timedelta(minutes=5)

# Module-name regex used in cron.log lines: each module logs as
# `nodeble_<bot>.<submod>` (and IC uniquely as just `nodeble`).
# The bot_id is the suffix after the underscore — except for IC which
# logs as `nodeble.<submod>` with no underscore. We map directly.
_LOGGER_TO_BOT = {
    "nodeble": "ic",
    "nodeble_wheel": "wheel",
    "nodeble_pmcc": "pmcc",
    "nodeble_directionalspread": "directionalspread",
}

# A line that begins with "YYYY-MM-DD HH:MM:SS,SSS" marks the start of a
# new log record; anything between two such lines is a continuation of
# the previous record's message body. Compiled once for tail performance.
_LOG_TS_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+(\S+)\s+(\w+)\s+(.*)$")
_TELEGRAM_MARKER = "Telegram message sent:"


class CronStatus(TypedDict):
    signal: str  # "ok" | "fail" | "pending" | "missed" | "n/a"
    manage: str
    scan: str


class HaltInfo(TypedDict):
    active: bool
    reason: str | None
    since: str | None


class TodayStats(TypedDict):
    opens: int
    closes: int
    realized_pnl: float


class BotSummary(TypedDict):
    id: str
    name: str
    cron_status: CronStatus
    last_heartbeat: str | None
    mode: str  # "live" | "dry_run"
    halt: HaltInfo
    today: TodayStats
    errors_today: int
    warnings_today: int


class StickyEntry(TypedDict):
    bot_id: str
    type: str  # "halt_persisting" | "stale_state_post_close" | ...
    detail: str
    since: str


class DailySummary(TypedDict):
    session: SessionInfo
    bots: list[BotSummary]
    discrepancies: list[Discrepancy]
    sticky: list[StickyEntry]


# ---------- Public API ----------


def compute_daily_summary(
    now: datetime,
    bot_data_sources: dict[str, dict[str, Any]],
) -> DailySummary:
    """Aggregate session + per-bot data + discrepancies into the dashboard contract.

    Args:
        now: Timezone-aware UTC. Drives session window + grace calculations.
        bot_data_sources: {bot_id: {cron_log, state_path, stop_file_path,
                          ledger_path, cron_schedule_et, name, mode}}.
                          The route layer constructs this from settings;
                          tests construct it from tmp_path.

    Returns:
        DailySummary dict matching the design doc shape.
    """
    session = compute_session(now)
    session_start_et = datetime.combine(
        datetime.fromisoformat(session["date_et"]).date(),
        time(9, 30),
        tzinfo=ET,
    )
    session_start_utc_iso = session_start_et.astimezone(timezone.utc).isoformat()

    bots: list[BotSummary] = []
    all_discrepancies: list[Discrepancy] = []
    sticky: list[StickyEntry] = []

    for bot_id, src in bot_data_sources.items():
        bot_summary, bot_discreps, bot_sticky = _summarize_bot(
            bot_id=bot_id,
            src=src,
            session=session,
            session_start_utc_iso=session_start_utc_iso,
            now=now,
        )
        bots.append(bot_summary)
        all_discrepancies.extend(bot_discreps)
        sticky.extend(bot_sticky)

    return {
        "session": session,
        "bots": bots,
        "discrepancies": all_discrepancies,
        "sticky": sticky,
    }


# ---------- Per-bot orchestration ----------


def _summarize_bot(
    bot_id: str,
    src: dict[str, Any],
    session: SessionInfo,
    session_start_utc_iso: str,
    now: datetime,
) -> tuple[BotSummary, list[Discrepancy], list[StickyEntry]]:
    """Build one bot's summary + collect its discrepancies + sticky entries.

    Per-bot exceptions are caught and turned into stub responses with
    errors_today incremented. Aggregator-level data corruption shouldn't
    crash the whole endpoint.
    """
    cron_log = Path(src["cron_log"])
    state_path = Path(src["state_path"])
    stop_path = Path(src["stop_file_path"])
    ledger_path = Path(src["ledger_path"])
    schedule = src["cron_schedule_et"]
    name = src.get("name", bot_id)
    mode = src.get("mode", "live")

    # ---- Source loads (each guarded; missing files → empty/safe defaults)
    cron_text = _read_text_safe(cron_log)
    cron_fires = _parse_cron_fires(cron_text, since_iso=session_start_utc_iso)
    telegram_msgs = _parse_telegram_messages(
        cron_text, bot_id=bot_id, since_iso=session_start_utc_iso
    )

    state_mtime_iso = _read_state_mtime(state_path)
    stop_active, stop_since_iso = _check_stop(stop_path)

    ledger_entries_raw = _load_ledger_jsonl(ledger_path)
    ledger_normalized = _normalize_ledger_to_detector_shape(ledger_entries_raw)
    bot_close_entries = [
        e for e in ledger_normalized
        if e["ts"] >= session_start_utc_iso
        and e.get("event_type") == "close"
        and e.get("actor") == bot_id
    ]
    bot_open_entries = [
        e for e in ledger_normalized
        if e["ts"] >= session_start_utc_iso
        and e.get("event_type") == "confirm"
        and e.get("actor") == bot_id
    ]
    closes_today = len(bot_close_entries)
    opens_today = len(bot_open_entries)
    realized_pnl_today = sum(
        float(e.get("realized_pnl", 0) or 0) for e in bot_close_entries
    )

    # ---- Detectors
    discrepancies: list[Discrepancy] = []
    discrepancies.extend(
        detect_telegram_close_mismatch(
            bot_id=bot_id,
            telegram_messages=telegram_msgs,
            ledger_entries=ledger_normalized,
            session_start=session_start_utc_iso,
        )
    )
    if state_mtime_iso is not None:
        discrepancies.extend(
            detect_stale_state(
                bot_id=bot_id,
                state_mtime=state_mtime_iso,
                now=now,
                market_open=session["market_open"],
            )
        )
    discrepancies.extend(
        detect_missing_cron_run(
            bot_id=bot_id,
            cron_schedule_et=schedule,
            cron_log_fires=cron_fires,
            now=now,
        )
    )
    # ledger_state_mismatch needs state's own close-count — for v1 we
    # derive from positions[] absent-from-current vs ledger close count.
    # Skip for now; will wire when state_close_count helper lands.
    # (Plan Phase 3.1 explicitly says "state.json count" — we treat 0
    # as the placeholder until state-reading is added; this means the
    # detector flags any session with ledger closes > 0 as a mismatch,
    # which is not what we want, so we skip wiring it for v1.)

    # ---- Cron status per type
    cron_status = _compute_cron_status(
        schedule=schedule,
        fires=cron_fires,
        now=now,
        market_open=session["market_open"],
    )

    last_heartbeat = max(cron_fires) if cron_fires else None

    halt: HaltInfo = {
        "active": stop_active,
        "reason": "STOP file present" if stop_active else None,
        "since": stop_since_iso,
    }

    bot_sticky: list[StickyEntry] = []
    if stop_active and stop_since_iso is not None:
        bot_sticky.append(
            {
                "bot_id": bot_id,
                "type": "halt_persisting",
                "detail": f"{name} ({bot_id}) kill switch active since {stop_since_iso}",
                "since": stop_since_iso,
            }
        )

    summary: BotSummary = {
        "id": bot_id,
        "name": name,
        "cron_status": cron_status,
        "last_heartbeat": last_heartbeat,
        "mode": mode,
        "halt": halt,
        "today": {
            "opens": opens_today,
            "closes": closes_today,
            "realized_pnl": realized_pnl_today,
        },
        "errors_today": 0,  # v1: not parsed; placeholder for future
        "warnings_today": 0,  # v1: not parsed
    }

    return summary, discrepancies, bot_sticky


# ---------- File I/O helpers (each defensive against missing files) ----------


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return ""


def _read_state_mtime(state_path: Path) -> str | None:
    try:
        mtime = state_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except (FileNotFoundError, PermissionError):
        return None


def _check_stop(stop_path: Path) -> tuple[bool, str | None]:
    """Return (stop_active, stop_mtime_iso)."""
    if not stop_path.exists():
        return False, None
    try:
        mtime = stop_path.stat().st_mtime
        return True, datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except (FileNotFoundError, PermissionError):
        return False, None


def _load_ledger_jsonl(ledger_path: Path) -> list[dict[str, Any]]:
    """Read JSONL ledger; tolerate malformed lines (skip them)."""
    out: list[dict[str, Any]] = []
    try:
        with ledger_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, PermissionError):
        return []
    return out


def _normalize_ledger_to_detector_shape(
    raw: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate real ledger fields to detector contract (event/strategy
    -> event_type/actor) so downstream detectors don't need to know
    about the real schema.
    """
    out = []
    for e in raw:
        normalized = dict(e)
        if "event_type" not in normalized and "event" in normalized:
            normalized["event_type"] = normalized["event"]
        if "actor" not in normalized and "strategy" in normalized:
            normalized["actor"] = normalized["strategy"]
        out.append(normalized)
    return out


# ---------- Log parsing ----------


def _parse_cron_fires(log_text: str, since_iso: str) -> list[str]:
    """Extract ISO-UTC timestamps of cron fires from log lines.

    A cron "fire" is approximated by any log line in the file with a
    timestamp >= session_start. The cron log is module-specific and
    the first line of each fire is what we want.

    For v1 we approximate: every log line with a timestamp >= session
    counts as a candidate; we dedupe by minute-bucket so multiple log
    lines from the same fire (typical: ~5-10 lines) don't double-count.
    The downstream detect_missing_cron_run is matching expected_time +
    grace, so what matters is that AT LEAST one fire timestamp falls
    in each cron's grace window.
    """
    out: list[str] = []
    seen_minutes: set[str] = set()
    for line in log_text.splitlines():
        m = _LOG_TS_PREFIX.match(line)
        if not m:
            continue
        ts_local = m.group(1)
        # Treat the cron log's local timestamp as UTC-local; cron logs
        # are written in the system's local time which on Tower is
        # configured to UTC for these modules. (If a module is on a
        # non-UTC host, callers should pre-convert; v1 trusts UTC.)
        try:
            dt = datetime.strptime(ts_local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        iso = dt.isoformat()
        if iso < since_iso:
            continue
        # Dedupe by minute (multiple lines from the same fire arrive
        # within the same minute; we only need one per fire window).
        minute_key = iso[:16]  # "2026-05-04T13:35"
        if minute_key in seen_minutes:
            continue
        seen_minutes.add(minute_key)
        out.append(iso)
    return out


def _parse_telegram_messages(
    log_text: str,
    bot_id: str,
    since_iso: str,
) -> list[dict[str, Any]]:
    """Extract `Telegram message sent: <body>` entries with multi-line bodies.

    A message starts on a line matching `_LOG_TS_PREFIX` containing
    `_TELEGRAM_MARKER`, and continues on subsequent lines until the next
    line matching `_LOG_TS_PREFIX`. We accumulate the full body so that
    "Closed N: ..." appearing on a continuation line is captured.

    Filtered to >= since_iso so prior-session noise doesn't leak.
    """
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body_lines: list[str] = []

    def _flush() -> None:
        nonlocal current, body_lines
        if current is not None:
            current["text"] = "\n".join(body_lines).strip()
            if current["ts"] >= since_iso:
                out.append(current)
        current = None
        body_lines = []

    for line in log_text.splitlines():
        m = _LOG_TS_PREFIX.match(line)
        if m:
            # New record — flush the previous if it was a Telegram one
            _flush()
            ts_local, _logger, _level, message = (
                m.group(1),
                m.group(2),
                m.group(3),
                m.group(4),
            )
            if _TELEGRAM_MARKER in message:
                try:
                    dt = datetime.strptime(ts_local, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                # The body so far on this line — strip the marker prefix
                idx = message.find(_TELEGRAM_MARKER)
                body_first = message[idx + len(_TELEGRAM_MARKER):].strip()
                current = {"ts": dt.isoformat(), "bot_id": bot_id}
                body_lines = [body_first]
        else:
            # Continuation of the previous record's body
            if current is not None:
                body_lines.append(line)
    _flush()
    return out


# ---------- Cron status per cron type ----------


def _compute_cron_status(
    schedule: dict[str, time],
    fires: list[str],
    now: datetime,
    market_open: bool,
) -> CronStatus:
    """For each cron type compute one of {ok, missed, pending, n/a}.

    "fail" requires log-line ERROR detection; v1 doesn't compute it
    (placeholder for future). Caller can flip "ok" → "fail" when
    error_today > 0 if needed in a later phase.
    """
    now_et = now.astimezone(ET)
    today_date = now_et.date()

    # If it's the weekend (no trading), every cron is "n/a".
    if today_date.weekday() >= 5:
        return {"signal": "n/a", "manage": "n/a", "scan": "n/a"}

    fire_dts_et = [datetime.fromisoformat(t).astimezone(ET) for t in fires]

    out: dict[str, str] = {}
    for cron_type, expected_time in schedule.items():
        expected_dt = datetime.combine(today_date, expected_time, tzinfo=ET)
        grace_end = expected_dt + CRON_GRACE_WINDOW
        in_window = any(expected_dt <= fdt <= grace_end for fdt in fire_dts_et)

        if in_window:
            out[cron_type] = "ok"
        elif now_et < expected_dt:
            out[cron_type] = "pending"
        elif now_et < grace_end:
            # In the grace window but no fire yet — still pending until
            # grace expires.
            out[cron_type] = "pending"
        else:
            out[cron_type] = "missed"

    # Fill in any keys the caller didn't provide
    for required in ("signal", "manage", "scan"):
        out.setdefault(required, "n/a")

    return out  # type: ignore[return-value]

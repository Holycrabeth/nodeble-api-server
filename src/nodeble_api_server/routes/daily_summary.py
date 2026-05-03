"""GET /api/v1/server/daily-summary — Dashboard "今日运营" surface.

Per UI 总监 dispatch 5/2 — single endpoint that aggregates 4-bot daily
operating state (cron firings, today's opens/closes/PnL, kill-switch
halts, plus 4 discrepancy detectors that catch 4/29-class silent
divergences within 60s).

Auth: standard Bearer token (router-level dependency).
Caching: `Cache-Control: no-store` per design doc — frontend polls 60s.
Latency budget: <200ms per design doc §"频率约束".

Spec ref: cto/reviews/2026-05-02-dashboard-daily-ops-card-design.md
Plan ref: plans/2026-05-02-dashboard-daily-ops-card-plan.md Phase 3.2
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Response

from nodeble_api_server.aggregators.daily_summary import compute_daily_summary
from nodeble_api_server.auth import require_bearer_token

router = APIRouter(
    prefix="/api/v1/server",
    dependencies=[Depends(require_bearer_token)],
)


# Bot directory map. The actual Tower-deployed path for each module's
# state + log + STOP file. Per CTO CLAUDE.md §"What You DON'T Do" we
# don't reach into module repos directly — we only read their state/log
# directories under ~/.<module>/.
_BOT_DIRS = {
    "ic": ".nodeble",
    "wheel": ".nodeble-wheel",
    "pmcc": ".nodeble-pmcc",
    "directionalspread": ".nodeble-directionalspread",
}

_BOT_NAMES = {
    "ic": "Iron Condor",
    "wheel": "Wheel",
    "pmcc": "PMCC",
    "directionalspread": "Credit Spread",
}

# Generic cron schedule (ET local). Per CTO CLAUDE.md the actual times
# are staggered slightly per module; v1 uses a conservative shared
# schedule aligned to the design doc spec. Future enhancement: read
# real schedule from each module's crontab file (~/.nodeble*/cron.txt
# or systemd-timer config).
_CRON_SCHEDULE_ET = {
    "signal": time(9, 35),
    "manage": time(9, 43),
    "scan": time(10, 15),
}

# Shared ARCH-16 ownership ledger (Tier 2). All 4 modules write here.
_LEDGER_PATH = ".nodeble-pnl/data/ownership_ledger.jsonl"


def _build_bot_data_sources(home: Path) -> dict:
    """Construct the bot_data_sources dict for compute_daily_summary."""
    ledger_path = str(home / _LEDGER_PATH)
    sources = {}
    for bot_id, dir_name in _BOT_DIRS.items():
        bot_root = home / dir_name
        sources[bot_id] = {
            "cron_log": str(bot_root / "logs" / "cron.log"),
            "state_path": str(bot_root / "data" / "state.json"),
            "stop_file_path": str(bot_root / "data" / "STOP"),
            "ledger_path": ledger_path,
            "cron_schedule_et": _CRON_SCHEDULE_ET,
            "name": _BOT_NAMES[bot_id],
            "mode": "live",  # v1: assume live; mode-drift detection deferred
        }
    return sources


@router.get("/daily-summary")
def get_daily_summary(response: Response) -> dict:
    """Return aggregated 4-bot daily-ops state.

    Response shape per design doc §B:
        {session, bots[], discrepancies[], sticky[]}

    No 503 / partial branch in v1: per-bot exceptions are caught inside
    `compute_daily_summary` and degrade to stub fields with errors_today
    incremented. The whole-aggregator failure case (e.g. corrupt session
    helper) is rare enough that letting FastAPI's default 500 handler
    take over is acceptable for v1. Phase 4+ can introduce a 503 +
    PartialAggregationError flow if operator experience demands it.
    """
    response.headers["Cache-Control"] = "no-store"

    now = datetime.now(timezone.utc)
    bot_data_sources = _build_bot_data_sources(Path.home())
    return compute_daily_summary(now=now, bot_data_sources=bot_data_sources)

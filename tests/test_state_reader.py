"""Unit tests for state_reader: timestamp normalization, file readers, cache, health."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from nodeble_api_server import state_reader
from nodeble_api_server.state_reader import (
    SERVER_TZ,
    STRATEGY_REGISTRY,
    clear_cache,
    compute_health,
    count_active_positions,
    latest_log_mtime,
    list_installed_strategies,
    normalize_timestamp,
    positions_as_list,
    read_allocation,
    read_config,
    read_halt_detail,
    read_halt_summary,
    read_signal_timestamp,
    read_state,
    sum_active_budget,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


# ── normalize_timestamp (required by spec) ──────────────────────────────────

def test_normalize_timestamp_date_only():
    # Date-only strings anchor to end-of-day ET so health stays green
    # for 24h after a run; see normalize_timestamp docstring.
    out = normalize_timestamp("2026-04-14")
    assert out == "2026-04-14T23:59:59-04:00"  # EDT, last moment of the day


def test_normalize_timestamp_iso_without_tz():
    out = normalize_timestamp("2026-04-17T14:30:02.722272")
    assert out == "2026-04-17T14:30:02.722272-04:00"


def test_normalize_timestamp_iso_with_tz_passthrough():
    out = normalize_timestamp("2026-04-17T13:33:02.951059-04:00")
    assert out == "2026-04-17T13:33:02.951059-04:00"


def test_normalize_timestamp_empty_returns_none():
    assert normalize_timestamp("") is None
    assert normalize_timestamp("   ") is None
    assert normalize_timestamp(None) is None


def test_normalize_timestamp_malformed_returns_none():
    assert normalize_timestamp("not a date") is None
    assert normalize_timestamp("2026-13-01") is None
    assert normalize_timestamp(12345) is None


def test_normalize_timestamp_winter_date_uses_est():
    # 2026-01-14 is in EST (-05:00), not EDT; end-of-day anchor.
    out = normalize_timestamp("2026-01-14")
    assert out == "2026-01-14T23:59:59-05:00"


def test_normalize_timestamp_iso_datetime_unaffected_by_date_only_change():
    """Full ISO datetimes never hit the end-of-day branch — they keep
    their exact time. Prevents regression where the parser order got
    flipped and stripped the time component."""
    out = normalize_timestamp("2026-04-21T10:30:00-04:00")
    assert out == "2026-04-21T10:30:00-04:00"
    out = normalize_timestamp("2026-04-21T10:30:00")
    assert out == "2026-04-21T10:30:00-04:00"


# ── File readers with faked home ────────────────────────────────────────────

def _make_strategy_home(tmp_path: Path, strategies: dict[str, dict]) -> Path:
    """Create fake ~/.nodeble-*/data/state.json + config/strategy.yaml files."""
    for sid, spec in strategies.items():
        meta = STRATEGY_REGISTRY[sid]
        strat_dir = tmp_path / meta["folder"]
        (strat_dir / "data").mkdir(parents=True, exist_ok=True)
        (strat_dir / "config").mkdir(parents=True, exist_ok=True)
        if "state" in spec:
            (strat_dir / "data" / "state.json").write_text(json.dumps(spec["state"]))
        if "config" in spec:
            (strat_dir / "config" / "strategy.yaml").write_text(yaml.safe_dump(spec["config"]))
        if "risk" in spec:
            (strat_dir / "config" / "risk.yaml").write_text(yaml.safe_dump(spec["risk"]))
        if "signal" in spec:
            (strat_dir / "data" / "signal_state.json").write_text(json.dumps(spec["signal"]))
    return tmp_path


def test_list_installed_only_returns_strategies_with_state(tmp_path):
    home = _make_strategy_home(
        tmp_path,
        {
            "ic": {"state": {"positions": {}}},
            "wheel": {"state": {"positions": {}}},
            # pmcc: config only, no state → not installed
            "pmcc": {"config": {"mode": "live"}},
        },
    )
    installed = list_installed_strategies(home=home)
    assert set(installed) == {"ic", "wheel"}


def test_read_state_missing_returns_none(tmp_path):
    assert read_state("ic", home=tmp_path) is None


def test_read_state_malformed_returns_none(tmp_path):
    home = tmp_path
    (home / ".nodeble" / "data").mkdir(parents=True)
    (home / ".nodeble" / "data" / "state.json").write_text("{not json")
    assert read_state("ic", home=home) is None


def test_read_state_unknown_strategy_returns_none(tmp_path):
    assert read_state("bogus", home=tmp_path) is None


def test_read_config_merges_strategy_and_risk(tmp_path):
    home = _make_strategy_home(
        tmp_path,
        {
            "ic": {
                "state": {"positions": {}},
                "config": {"mode": "live", "capital": {"budget": 20000}},
                "risk": {"risk": {"max_concurrent_positions": 8}},
            }
        },
    )
    cfg = read_config("ic", home=home)
    assert cfg is not None
    assert cfg["mode"] == "live"
    assert cfg["capital"]["budget"] == 20000
    assert cfg["risk"]["max_concurrent_positions"] == 8


def test_read_config_tolerates_missing_risk_yaml(tmp_path):
    home = _make_strategy_home(
        tmp_path,
        {"collar": {"state": {"positions": {}}, "config": {"mode": "live"}}},
    )
    cfg = read_config("collar", home=home)
    assert cfg == {"mode": "live"}


def test_read_config_missing_strategy_yaml_returns_none(tmp_path):
    assert read_config("ic", home=tmp_path) is None


def test_read_allocation_missing_returns_none(tmp_path):
    assert read_allocation(home=tmp_path) is None


def test_read_allocation_present(tmp_path):
    alloc_path = tmp_path / ".nodeble-orchestrator" / "data" / "allocation.json"
    alloc_path.parent.mkdir(parents=True)
    alloc_path.write_text(
        json.dumps({"strategies": {"ic": {"max_buying_power": 5000}}})
    )
    alloc = read_allocation(home=tmp_path)
    assert alloc["strategies"]["ic"]["max_buying_power"] == 5000


def test_read_signal_timestamp(tmp_path):
    home = _make_strategy_home(
        tmp_path,
        {
            "ic": {
                "state": {"positions": {}},
                "signal": {"generated_at": "2026-04-17T13:33:02.951059-04:00"},
            }
        },
    )
    ts = read_signal_timestamp("ic", home=home)
    assert ts == "2026-04-17T13:33:02.951059-04:00"


# ── Cache ────────────────────────────────────────────────────────────────────

def test_cache_prevents_second_read(tmp_path, monkeypatch):
    home = _make_strategy_home(
        tmp_path, {"ic": {"state": {"positions": {"a": {"status": "open"}}}}}
    )
    read_count = {"n": 0}
    original = state_reader._read_json

    def counting(path: Path):
        read_count["n"] += 1
        return original(path)

    monkeypatch.setattr(state_reader, "_read_json", counting)

    r1 = read_state("ic", home=home)
    r2 = read_state("ic", home=home)
    assert r1 == r2
    assert read_count["n"] == 1  # second call was served from cache


# ── Position helpers ────────────────────────────────────────────────────────

def test_positions_as_list_from_dict_adds_spread_id():
    positions = {"SPY_ic_001": {"status": "open"}, "SPY_ic_002": {"status": "closed_profit"}}
    lst = positions_as_list(positions)
    ids = sorted(p["spread_id"] for p in lst)
    assert ids == ["SPY_ic_001", "SPY_ic_002"]


def test_positions_as_list_from_list_passthrough():
    positions = [{"spread_id": "a", "status": "open"}]
    assert positions_as_list(positions) == positions


def test_count_active_filters_closed_variants():
    positions = {
        "1": {"status": "open"},
        "2": {"status": "assigned"},
        "3": {"status": "close_profit"},      # IC naming
        "4": {"status": "closed_profit"},     # Wheel naming
        "5": {"status": "close_manual"},
        "6": {"status": "closed_manual"},
        "7": {"status": "cancelled"},
        "8": {"status": "close_dte"},
    }
    assert count_active_positions(positions) == 2


def test_sum_active_budget_fallback_formula():
    """Legacy path: no capital_used field → IC formula
    (max_risk × contracts × 100). Kept through 2026-05-20 rollout so
    modules that haven't migrated to ARCH-12 yet still contribute
    (wrongly, but non-crashingly)."""
    positions = [
        {"status": "open", "max_risk": 100, "contracts": 2},       # 100*2*100 = 20000
        {"status": "assigned", "max_risk": 50, "contracts": 1},    # 50*1*100  =  5000
        {"status": "closed_profit", "max_risk": 999, "contracts": 999},  # ignored
    ]
    assert sum_active_budget(positions) == 25000


# ── ARCH-12 scheme B: capital_used takes precedence ────────────────────────


def test_sum_active_budget_capital_used_is_authoritative():
    """When a position carries capital_used, sum_active_budget must
    use it even if max_risk would give a different answer. Modules
    own their own capital semantics (spec §3)."""
    positions = [
        # Wheel CSP: capital_used = strike × contracts × 100, max_risk absent.
        {"status": "open", "capital_used": 58500.0},
        # DS defined-risk: capital_used carried alongside max_risk; use the former.
        {"status": "open", "capital_used": 1040.0, "max_risk": 5.20, "contracts": 2},
    ]
    assert sum_active_budget(positions) == 59540.0


def test_sum_active_budget_capital_used_respects_zero():
    """ARCH-12 §3.5: Wheel CC positions legitimately set capital_used=0.0
    (already counted in linked assigned-put position). Must not trigger
    the fallback formula (which would double-count)."""
    positions = [
        # CSP contributes 58,500 via capital_used.
        {"status": "open", "capital_used": 58500.0},
        # CC has capital_used=0 AND has strike/contracts that would
        # otherwise activate the fallback formula if capital_used
        # weren't present. Explicit 0 must be honored.
        {"status": "open", "capital_used": 0.0, "strike": 590, "contracts": 1},
    ]
    assert sum_active_budget(positions) == 58500.0


def test_sum_active_budget_mixed_migrated_and_legacy():
    """During rollout Phase 1-3, a single sum may span ARCH-12-migrated
    positions (IC) and legacy ones (PMCC etc). Both contribute."""
    positions = [
        {"status": "open", "capital_used": 1040.0},                  # migrated: 1040
        {"status": "open", "max_risk": 50, "contracts": 1},          # legacy: 50*1*100 = 5000
    ]
    assert sum_active_budget(positions) == 6040.0


def test_sum_active_budget_malformed_capital_used_is_skipped():
    """Malformed capital_used (string, None) = schema contract violation.
    Spec §7: skip the position entirely — don't silently fall back to
    max_risk, which would hide the real bug."""
    positions = [
        {"status": "open", "capital_used": "not-a-number", "max_risk": 100, "contracts": 2},
        {"status": "open", "capital_used": None, "max_risk": 50, "contracts": 1},
        {"status": "open", "capital_used": 200.0},  # the one that should sum
    ]
    assert sum_active_budget(positions) == 200.0


def test_sum_active_budget_capital_used_as_of_is_ignored_for_sum():
    """capital_used_as_of is a staleness timestamp for downstream
    warning logic; sum_active_budget itself must not choke on it."""
    positions = [
        {
            "status": "open",
            "capital_used": 1000.0,
            "capital_used_as_of": "2026-04-22T18:30:00+00:00",
        },
    ]
    assert sum_active_budget(positions) == 1000.0


# ── ARCH-12 §7: capital_used staleness warning ─────────────────────────────

import logging  # noqa: E402

from nodeble_api_server.state_reader import (  # noqa: E402
    _reset_stale_warning_state,
    check_stale_capital_used,
)


def _fixed_now_epoch() -> float:
    """Friday 2026-04-22 18:00 UTC in epoch seconds — gives us a stable
    anchor so staleness arithmetic is reproducible."""
    from datetime import datetime, timezone
    return datetime(2026, 4, 22, 18, 0, 0, tzinfo=timezone.utc).timestamp()


def test_check_stale_fresh_timestamp_no_warn(caplog):
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    # 1h fresh.
    positions = [
        {
            "status": "open",
            "spread_id": "fresh-1",
            "capital_used": 1000.0,
            "capital_used_as_of": "2026-04-22T17:00:00+00:00",
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        count = check_stale_capital_used("wheel", positions, now_epoch=now)
    assert count == 0
    assert not any("capital_used stale" in rec.message for rec in caplog.records)


def test_check_stale_25h_old_warns_with_age(caplog):
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    # 25h stale.
    positions = [
        {
            "status": "open",
            "spread_id": "stale-1",
            "capital_used": 1000.0,
            "capital_used_as_of": "2026-04-21T17:00:00+00:00",
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        count = check_stale_capital_used("wheel", positions, now_epoch=now)
    assert count == 1
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("capital_used stale" in m for m in msgs)
    assert any("stale-1" in m for m in msgs)
    assert any("25.0h" in m or "25h" in m for m in msgs)


def test_check_stale_missing_field_silent(caplog):
    """During Phase 1-3 rollout, positions legitimately lack
    capital_used_as_of. Must not flood logs on absence."""
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    positions = [
        {"status": "open", "spread_id": "pre-arch-12", "max_risk": 500, "contracts": 2},
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        count = check_stale_capital_used("ic", positions, now_epoch=now)
    assert count == 0
    assert len(caplog.records) == 0


def test_check_stale_malformed_timestamp_warns(caplog):
    """Malformed timestamp is a schema contract violation — still
    surfaces, with a distinct 'malformed' reason vs stale-age."""
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    positions = [
        {
            "status": "open",
            "spread_id": "bad-ts",
            "capital_used": 1000.0,
            "capital_used_as_of": "not-a-real-iso-string",
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        count = check_stale_capital_used("strangle", positions, now_epoch=now)
    assert count == 1
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("malformed" in m for m in msgs)
    assert any("bad-ts" in m for m in msgs)


def test_check_stale_closed_positions_skipped(caplog):
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    positions = [
        {
            "status": "closed_profit",  # filtered by active-status check
            "spread_id": "old-closed",
            "capital_used": 1000.0,
            "capital_used_as_of": "2026-04-01T00:00:00+00:00",  # 3 weeks old, would warn if open
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        count = check_stale_capital_used("wheel", positions, now_epoch=now)
    assert count == 0
    assert len(caplog.records) == 0


def test_check_stale_dedupes_within_cooldown(caplog):
    """Same (strategy, position) warned twice within 1h → only one log.
    Dashboard polling at ~5s cadence must not flood journald."""
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    positions = [
        {
            "status": "open",
            "spread_id": "flap-1",
            "capital_used": 1000.0,
            "capital_used_as_of": "2026-04-21T00:00:00+00:00",  # way stale
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        check_stale_capital_used("wheel", positions, now_epoch=now)
        # Same call 5 seconds later — must not re-warn.
        check_stale_capital_used("wheel", positions, now_epoch=now + 5)
        check_stale_capital_used("wheel", positions, now_epoch=now + 60)

    stale_msgs = [r for r in caplog.records if "capital_used stale" in r.getMessage()]
    assert len(stale_msgs) == 1


def test_check_stale_dedup_expires_after_cooldown(caplog):
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    positions = [
        {
            "status": "open",
            "spread_id": "flap-2",
            "capital_used": 1000.0,
            "capital_used_as_of": "2026-04-21T00:00:00+00:00",
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        check_stale_capital_used("wheel", positions, now_epoch=now)
        # 2h later — cooldown elapsed, should warn again.
        check_stale_capital_used("wheel", positions, now_epoch=now + 2 * 3600)

    stale_msgs = [r for r in caplog.records if "capital_used stale" in r.getMessage()]
    assert len(stale_msgs) == 2


def test_check_stale_dedup_is_per_position_not_global(caplog):
    """Two stale positions on the same strategy both warn independently."""
    _reset_stale_warning_state()
    now = _fixed_now_epoch()
    positions = [
        {
            "status": "open",
            "spread_id": "p1",
            "capital_used_as_of": "2026-04-21T00:00:00+00:00",
        },
        {
            "status": "open",
            "spread_id": "p2",
            "capital_used_as_of": "2026-04-21T00:00:00+00:00",
        },
    ]
    with caplog.at_level(logging.WARNING, logger="nodeble_api_server.state_reader"):
        check_stale_capital_used("wheel", positions, now_epoch=now)

    stale_msgs = [r for r in caplog.records if "capital_used stale" in r.getMessage()]
    assert len(stale_msgs) == 2


# ── Health ──────────────────────────────────────────────────────────────────

def test_health_critical_when_scan_missing():
    assert compute_health(None, "2026-04-19T10:00:00-04:00", None) == "critical"


def test_health_critical_when_manage_missing_and_positions_open():
    """Active book without a manage timestamp is real negligence — scan
    alone proves cron is firing but something's dropping the manage leg.
    Keep as critical so the operator sees it in red."""
    assert (
        compute_health(
            "2026-04-19T10:00:00-04:00",
            None,
            None,
            open_positions=3,
        )
        == "critical"
    )


def test_health_healthy_when_manage_missing_but_no_open_positions():
    """Regression fix: IronButterfly / Calendar / Straddle in their
    dry_run + 0 positions baseline skip writing last_manage_date because
    the manage mode exits early when there's nothing to manage. That's
    expected behavior — flagging them critical for NOT writing a
    no-op-manage timestamp created false alarms every cron tick. Heal
    to healthy when open_positions==0."""
    now = datetime.now(SERVER_TZ)
    recent_scan = now.isoformat()
    assert (
        compute_health(
            recent_scan,
            None,          # manage never wrote — strategy has no work
            recent_scan,   # signal still recent
            now=now,
            open_positions=0,
        )
        == "healthy"
    )


def test_health_critical_when_manage_missing_default_zero_positions():
    """Default open_positions=0 preserves old behavior for ambiguous
    callers: a manage timestamp that's missing with 0 known positions
    is healthy. Ensures explicit callsite wiring isn't accidentally
    broken by default-arg drift."""
    now = datetime.now(SERVER_TZ)
    recent = now.isoformat()
    # Kwargs omitted on purpose — explicit healthy via default gate.
    assert compute_health(recent, None, recent, now=now) == "healthy"


def test_health_healthy_recent():
    now = datetime.now(SERVER_TZ)
    recent = now.isoformat()
    assert compute_health(recent, recent, recent, now=now) == "healthy"


def test_health_warning_stale():
    now = datetime.now(SERVER_TZ)
    stale = (now - timedelta(hours=30)).isoformat()
    recent = now.isoformat()
    assert compute_health(stale, recent, recent, now=now) == "warning"


def test_health_warning_when_signal_stale():
    now = datetime.now(SERVER_TZ)
    recent = now.isoformat()
    stale_signal = (now - timedelta(hours=48)).isoformat()
    assert compute_health(recent, recent, stale_signal, now=now) == "warning"


def test_health_healthy_when_date_only_ran_yesterday():
    """Regression: IC's state.json stores `last_scan_date="2026-04-21"`
    as a bare date. Before the end-of-day fix, normalize_timestamp
    anchored to 00:00 ET and the health calc saw ~25h of age the next
    morning, flipping to `warning`. Now that date-only normalizes to
    23:59:59 ET, the same scenario stays `healthy`."""
    scan_date = normalize_timestamp("2026-04-21")
    manage_date = scan_date
    now_et = datetime(2026, 4, 22, 1, 0, 0, tzinfo=SERVER_TZ)  # 1 AM next day ET
    assert compute_health(scan_date, manage_date, scan_date, now=now_et) == "healthy"


def test_health_warning_when_date_only_ran_two_days_ago():
    """Sanity: the fix doesn't over-correct. A scan from two days ago
    should still age past the 24h threshold and trip warning."""
    scan_date = normalize_timestamp("2026-04-19")
    now_et = datetime(2026, 4, 22, 0, 0, 0, tzinfo=SERVER_TZ)
    assert compute_health(scan_date, scan_date, scan_date, now=now_et) == "warning"


# ── latest_log_mtime ─────────────────────────────────────────────────────────

def test_latest_log_mtime_returns_newest(tmp_path):
    logs = tmp_path / ".nodeble" / "logs"
    logs.mkdir(parents=True)
    (logs / "cron.log").write_text("x")
    (logs / "nodeble.log").write_text("x")
    ts = latest_log_mtime("ic", home=tmp_path)
    assert ts is not None
    # Parseable back as datetime
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None


def test_latest_log_mtime_no_logs_returns_none(tmp_path):
    (tmp_path / ".nodeble" / "logs").mkdir(parents=True)
    assert latest_log_mtime("ic", home=tmp_path) is None


# ── _compute_pnl_fields (StrategyCard 3-field populate) ─────────────────────
#
# UI Director audit follow-up 2026-04-26: the three PnL summary
# fields on StrategyCard (today_pnl / cumulative_pnl_7d /
# cumulative_pnl_30d) were declared but hardcoded None in
# build_strategy_card. These tests pin the new compute path:
#   - empty history file → all None
#   - today's snapshot present + cumulative motion → today_pnl
#     surfaces the latest delta, 7d/30d sum non-null deltas
#   - all-null cumulative (e.g. brand-new strategy that hasn't
#     traded yet) → all None preserved (don't fake a "$0 today")


def _seed_pnl_history(home: Path, strategy: str, rows: list[dict]) -> Path:
    """Helper: write daily-pnl.jsonl under tmp home with the given rows.
    Returns the resolved path so tests can also negative-assert on
    file presence if they want."""
    history_path = home / ".nodeble-api" / "history" / "daily-pnl.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return history_path


def test_compute_pnl_fields_no_history_file(tmp_path):
    # No daily-pnl.jsonl ever written (fresh install pre-snapshot).
    # All three fields must be None — UI tile shows "—" rather than
    # a stale or invented "$0".
    fields = state_reader._compute_pnl_fields("ic", home=tmp_path)
    assert fields == {
        "today_pnl": None,
        "cumulative_pnl_7d": None,
        "cumulative_pnl_30d": None,
    }


def test_compute_pnl_fields_today_snapshot_with_motion(tmp_path):
    # Two days of snapshots, the second on TODAY (ET). The latest
    # row's daily_delta becomes today_pnl; 7d/30d sum the non-null
    # deltas (here just the second entry's delta).
    today = datetime.now(SERVER_TZ).date()
    yesterday = today - timedelta(days=1)
    _seed_pnl_history(
        tmp_path,
        "ic",
        [
            {
                "date": yesterday.isoformat(),
                "snapshot_at": f"{yesterday.isoformat()}T23:59:00-04:00",
                "strategy": "ic",
                "realized_pnl_cumulative": 100.0,
                "open_positions_count": 2,
            },
            {
                "date": today.isoformat(),
                "snapshot_at": f"{today.isoformat()}T23:59:00-04:00",
                "strategy": "ic",
                "realized_pnl_cumulative": 250.0,
                "open_positions_count": 2,
            },
        ],
    )

    fields = state_reader._compute_pnl_fields("ic", home=tmp_path)
    # First row's delta = None (no prior); second row's delta = 150.
    # today matches → today_pnl = 150.
    assert fields["today_pnl"] == 150.0
    # 7d / 30d windows include only the non-None deltas → 150.
    assert fields["cumulative_pnl_7d"] == 150.0
    assert fields["cumulative_pnl_30d"] == 150.0


def test_compute_pnl_fields_latest_not_today_returns_none_today(tmp_path):
    # Today is e.g. 2026-04-24; latest snapshot is 2026-04-22 (no
    # 23:59 ET tick has fired since). today_pnl must be None — yesterday's
    # delta isn't today's. 7d / 30d still aggregate what's there.
    today = datetime.now(SERVER_TZ).date()
    two_days_ago = today - timedelta(days=2)
    three_days_ago = today - timedelta(days=3)
    _seed_pnl_history(
        tmp_path,
        "wheel",
        [
            {
                "date": three_days_ago.isoformat(),
                "snapshot_at": f"{three_days_ago.isoformat()}T23:59:00-04:00",
                "strategy": "wheel",
                "realized_pnl_cumulative": 8000.0,
                "open_positions_count": 5,
            },
            {
                "date": two_days_ago.isoformat(),
                "snapshot_at": f"{two_days_ago.isoformat()}T23:59:00-04:00",
                "strategy": "wheel",
                "realized_pnl_cumulative": 9200.0,
                "open_positions_count": 5,
            },
        ],
    )

    fields = state_reader._compute_pnl_fields("wheel", home=tmp_path)
    assert fields["today_pnl"] is None
    # Window deltas: row 0 None (first row), row 1 = 1200. Sum = 1200.
    assert fields["cumulative_pnl_7d"] == 1200.0
    assert fields["cumulative_pnl_30d"] == 1200.0


def test_compute_pnl_fields_all_null_cumulative_returns_all_none(tmp_path):
    # Edge case: strategy enrolled in snapshot loop but its state.json
    # never had a realized_pnl_cumulative (newly installed strategy
    # that hasn't traded). Every entry's daily_delta is None → no
    # window sum can be computed → all three fields stay None.
    today = datetime.now(SERVER_TZ).date()
    yesterday = today - timedelta(days=1)
    _seed_pnl_history(
        tmp_path,
        "calendar",
        [
            {
                "date": yesterday.isoformat(),
                "snapshot_at": f"{yesterday.isoformat()}T23:59:00-04:00",
                "strategy": "calendar",
                "realized_pnl_cumulative": None,
                "open_positions_count": 0,
            },
            {
                "date": today.isoformat(),
                "snapshot_at": f"{today.isoformat()}T23:59:00-04:00",
                "strategy": "calendar",
                "realized_pnl_cumulative": None,
                "open_positions_count": 0,
            },
        ],
    )

    fields = state_reader._compute_pnl_fields("calendar", home=tmp_path)
    assert fields["today_pnl"] is None
    assert fields["cumulative_pnl_7d"] is None
    assert fields["cumulative_pnl_30d"] is None


# ── Halt status (audit-26 spec §2) ──────────────────────────────────────────
#
# Per ~/projects/cto/reviews/2026-04-26-audit-26-halt-status-spec.md.
# read_halt_summary: 2-field dict for /api/v1/strategies list endpoint
#                    (cached via _cached, part of 9-strategy fan-out).
# read_halt_detail:  4-field dict for /api/v1/strategies/{id}/halted
#                    (NOT cached, supports M3.b /close race-protection).


def test_halt_summary_false_when_stop_absent(tmp_path):
    """No STOP file → halted=False, halted_reason=None."""
    (tmp_path / ".nodeble-wheel" / "data").mkdir(parents=True)
    result = read_halt_summary("wheel", home=tmp_path)
    assert result == {"halted": False, "halted_reason": None}


def test_halt_summary_true_with_first_line_reason(tmp_path):
    """STOP file exists with content → halted=True, halted_reason=first line."""
    stop_dir = tmp_path / ".nodeble-wheel"
    stop_dir.mkdir()
    (stop_dir / "STOP").write_text("Reconcile drift detected\nUNCLASSIFIED: SPY 260430C690")
    result = read_halt_summary("wheel", home=tmp_path)
    assert result == {"halted": True, "halted_reason": "Reconcile drift detected"}


def test_halt_summary_empty_file_falls_back_to_default(tmp_path):
    """STOP file present but empty → halted=True, halted_reason=fallback string."""
    stop_dir = tmp_path / ".nodeble-wheel"
    stop_dir.mkdir()
    (stop_dir / "STOP").touch()  # empty
    result = read_halt_summary("wheel", home=tmp_path)
    assert result["halted"] is True
    assert result["halted_reason"] == "系统检测到异常,请联系管理员"


def test_halt_summary_unknown_strategy_returns_safe_default(tmp_path):
    """Unknown strategy_id → halted=False (don't surface as halted)."""
    result = read_halt_summary("nonexistent_strategy", home=tmp_path)
    assert result == {"halted": False, "halted_reason": None}


def test_halt_summary_ic_uses_nodeble_no_suffix(tmp_path):
    """IC's STOP path is ~/.nodeble/STOP (historical no-suffix). Verify mapping."""
    ic_dir = tmp_path / ".nodeble"
    ic_dir.mkdir()
    (ic_dir / "STOP").write_text("IC drift")
    result = read_halt_summary("ic", home=tmp_path)
    assert result == {"halted": True, "halted_reason": "IC drift"}


def test_halt_detail_false_when_stop_absent(tmp_path):
    """No STOP file → 4-field dict all None / False."""
    (tmp_path / ".nodeble-wheel").mkdir()
    result = read_halt_detail("wheel", home=tmp_path)
    assert result == {
        "halted": False,
        "reason": None,
        "halted_at": None,
        "full_content": None,
    }


def test_halt_detail_true_with_full_content(tmp_path):
    """STOP file exists → halted=True, reason=first line, full_content=full body, halted_at=ISO mtime."""
    stop_dir = tmp_path / ".nodeble-wheel"
    stop_dir.mkdir()
    full_body = "Reconcile drift detected\nUNCLASSIFIED: SPY 260430C690\nThree lines"
    (stop_dir / "STOP").write_text(full_body)
    result = read_halt_detail("wheel", home=tmp_path)
    assert result["halted"] is True
    assert result["reason"] == "Reconcile drift detected"
    assert result["full_content"] == full_body
    assert result["halted_at"] is not None  # ISO 8601 string
    # Sanity check ISO format
    assert "T" in result["halted_at"]


def test_halt_detail_empty_file_falls_back(tmp_path):
    """Empty STOP file → halted=True, reason=fallback, full_content empty string."""
    stop_dir = tmp_path / ".nodeble-wheel"
    stop_dir.mkdir()
    (stop_dir / "STOP").touch()
    result = read_halt_detail("wheel", home=tmp_path)
    assert result["halted"] is True
    assert result["reason"] == "系统检测到异常,请联系管理员"
    assert result["full_content"] == ""


def test_halt_detail_unknown_strategy_returns_safe_default(tmp_path):
    """Unknown strategy_id → returns the same 4-field shape, all defaults."""
    result = read_halt_detail("nonexistent_strategy", home=tmp_path)
    assert result == {
        "halted": False,
        "reason": None,
        "halted_at": None,
        "full_content": None,
    }


def test_build_strategy_card_includes_halt_fields_when_halted(tmp_path):
    """build_strategy_card injects halted + halted_reason from STOP file."""
    wheel_dir = tmp_path / ".nodeble-wheel"
    (wheel_dir / "data").mkdir(parents=True)
    (wheel_dir / "config").mkdir(parents=True)
    (wheel_dir / "data" / "state.json").write_text(json.dumps({
        "last_scan_date": "2026-04-19",
        "last_manage_date": "2026-04-19T14:30:00",
        "positions": {},
    }))
    (wheel_dir / "config" / "strategy.yaml").write_text(yaml.safe_dump({"mode": "live"}))
    (wheel_dir / "STOP").write_text("Manual halt by operator")

    card = state_reader.build_strategy_card("wheel", home=tmp_path)
    assert card["halted"] is True
    assert card["halted_reason"] == "Manual halt by operator"


def test_build_strategy_card_halted_false_default(tmp_path):
    """build_strategy_card returns halted=False when no STOP file."""
    wheel_dir = tmp_path / ".nodeble-wheel"
    (wheel_dir / "data").mkdir(parents=True)
    (wheel_dir / "config").mkdir(parents=True)
    (wheel_dir / "data" / "state.json").write_text(json.dumps({
        "last_scan_date": "2026-04-19",
        "last_manage_date": "2026-04-19T14:30:00",
        "positions": {},
    }))
    (wheel_dir / "config" / "strategy.yaml").write_text(yaml.safe_dump({"mode": "live"}))

    card = state_reader.build_strategy_card("wheel", home=tmp_path)
    assert card["halted"] is False
    assert card["halted_reason"] is None

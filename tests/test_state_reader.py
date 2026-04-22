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


# ── Health ──────────────────────────────────────────────────────────────────

def test_health_critical_when_scan_missing():
    assert compute_health(None, "2026-04-19T10:00:00-04:00", None) == "critical"


def test_health_critical_when_manage_missing():
    assert compute_health("2026-04-19T10:00:00-04:00", None, None) == "critical"


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

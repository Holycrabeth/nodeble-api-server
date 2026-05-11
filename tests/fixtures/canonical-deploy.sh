#!/bin/bash
# Canonical-fixture deploy.sh — Path A hermetic install_runner smoke
# (CTO 2026-05-10 dispatch — verify install_runner against full 10-STEP
# contract sequence, NOT 2-3 STEP synthetic minimums).
#
# Spec: ~/projects/cto/reviews/2026-05-05-deploy-sh-non-interactive-contract.md §4
# Reference: ~/projects/nodeble-wheel/deploy/deploy.sh (Wheel canonical, 1100-line
#            real impl; this fixture is a 10-STEP minimal subset that emits
#            contract-conformant stdout shapes only — no actual venv/pip/cron).
#
# Modes (passed as $1):
#   happy            — full 10-STEP + 3 RESULT_* + STATUS: success (default)
#   already_installed — minimal STEPs + STATUS: already_installed (idempotent re-run)
#   fail_clone       — STEP 4 (clone-repo) emits ✗ + STATUS: failure: clone_failed
#   slow             — long sleeps (timeout test target; budget-busting via SIGTERM)
#   log_spam         — happy path + extra bare-line stdout/stderr (log_tail capture)
#   contract_violation — exit 0 with NO STATUS terminal (drift detection)
#
# Each STEP has a small sleep (50ms) between start + end so install_runner's
# duration_ms tracking can capture non-zero values without being slow in CI.

set -e

MODE="${1:-happy}"

emit_step()      { echo "STEP: $1"; }
emit_step_ok()   { echo "STEP: $1 ✓"; sleep 0.05; }
emit_step_fail() { echo "STEP: $1 ✗ $2"; }
emit_status()    { echo "STATUS: $*"; }
emit_result()    { echo "RESULT_$1: $2"; }

# --- 10-STEP canonical sequence (mirrors Wheel STEP order per spec §4.3) ---
# IDs are kebab-case lowercase. Per-STEP:
#   1. emit start line
#   2. (real deploy.sh would do work here — fixture just sleeps 50ms)
#   3. emit end line (✓ on success, ✗ on failure)
# Fixture preserves the start→work→end pairing so duration_ms is real
# (not a synthetic instantaneous sequence).

run_step() {
    local step_id="$1"
    emit_step "$step_id"
    sleep 0.05
    emit_step_ok "$step_id"
}

case "$MODE" in
    happy)
        run_step "prereq-check-os"
        run_step "prereq-check-python"
        run_step "prereq-tiger-creds"
        run_step "clone-repo"
        run_step "venv-create"
        run_step "venv-install-deps"
        run_step "config-write-strategy-yaml"
        run_step "cron-install-signal"
        run_step "systemd-service-start"
        run_step "post-install-smoke"
        emit_result "VERSION" "0.7.2"
        emit_result "INSTALLED_AT" "2026-05-10T13:30:00Z"
        emit_result "SERVICE_NAME" "nodeble-wheel-bot.service"
        emit_status "success"
        ;;

    already_installed)
        # Idempotent re-run: spec §5.1 says emit STATUS: already_installed
        # exit 0 when all steps would be no-ops. Real impl runs an
        # idempotency probe early and short-circuits; fixture mimics that.
        emit_step "idempotency-probe"
        sleep 0.05
        emit_step_ok "idempotency-probe"
        emit_result "VERSION" "0.7.2"
        emit_result "INSTALLED_AT" "2026-05-10T13:30:00Z"
        emit_result "SERVICE_NAME" "nodeble-wheel-bot.service"
        emit_status "already_installed"
        ;;

    fail_clone)
        run_step "prereq-check-os"
        run_step "prereq-check-python"
        run_step "prereq-tiger-creds"
        emit_step "clone-repo"
        sleep 0.05
        emit_step_fail "clone-repo" "git clone failed: remote not found"
        emit_status "failure: clone_failed"
        exit 11
        ;;

    slow)
        # Long sleep beyond any reasonable test budget — install_runner's
        # total_budget_ms cap should fire SIGTERM before we ever emit STATUS.
        emit_step "prereq-check-os"
        sleep 30
        emit_step_ok "prereq-check-os"
        emit_status "success"
        ;;

    log_spam)
        # Happy path with extra bare-line stdout (info logs) + stderr (warn logs).
        # Verifies install_runner captures both sources into state.json log_tail.
        run_step "prereq-check-os"
        echo "downloading 12345 bytes (this is a bare stdout line)"
        echo "warning: deprecated option used (this is stderr)" >&2
        run_step "prereq-check-python"
        echo "another bare stdout line"
        run_step "post-install-smoke"
        emit_result "VERSION" "0.7.2"
        emit_status "success"
        ;;

    contract_violation)
        # Exit 0 with no STATUS terminal — install_runner MUST treat as failed
        # per spec contract violation handling (run_install final-status logic).
        run_step "prereq-check-os"
        # NO emit_status — exit 0 ungracefully
        ;;

    *)
        echo "STEP: fixture-arg-error ✗ unknown mode: $MODE" >&2
        emit_status "failure: fixture_unknown_mode: $MODE"
        exit 99
        ;;
esac

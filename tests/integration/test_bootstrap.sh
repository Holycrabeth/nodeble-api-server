#!/usr/bin/env bash
# tests/integration/test_bootstrap.sh — Phase F''' Docker acceptance.
#
# Per ratified spec ~/projects/cto/reviews/2026-05-05-bootstrap-sh-
# design.md §9.1 — verifies bootstrap.sh is wire-grammar-clean +
# idempotent + OS-gated across 3 reference distros.
#
# v1 acceptance scope (fire-now per L1 §1):
#   • Syntax (bash -n) on all 3 distros — catches OS-specific
#     bash-version differences (Debian 12 ships bash 5.2 vs Ubuntu
#     22.04 bash 5.1; Ubuntu 24.04 bash 5.2).
#   • --help head sanity (catches stray `set -e` early exits).
#   • Bad-flag → exit 2 (CLI contract per spec §5).
#   • --dry-run idempotency-probe + OS-check + tooling-probe paths.
#     Full install (--no-dry-run, side-effecting) requires systemd-
#     enabled container OR fresh VM — out of v1 Docker scope. v1
#     full-install acceptance = Yongtao Mac smoke 5/28 on fresh
#     Vultr VM per pivot §4.
#
# Failure-mode coverage (per spec §9.2):
#   • Unsupported OS (rocky / alpine) → STATUS: failure: unsupported_os
#   • Network none → defensive failure path (best-effort)
#
# Requires: Docker daemon on the host runner (Tower has it; GitHub
# Actions ubuntu-latest does). Run:
#
#   bash tests/integration/test_bootstrap.sh
#
# Exit 0 on full pass, exit 1 on any test failure.

set -o errexit
set -o nounset
set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BOOTSTRAP_SH="$REPO_ROOT/scripts/bootstrap.sh"
LOG_DIR="$(mktemp -d -t bootstrap-test-XXXXXX)"

if [ ! -f "$BOOTSTRAP_SH" ]; then
    echo "FAIL: $BOOTSTRAP_SH not found" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "SKIP: docker not available on this host" >&2
    exit 0
fi

# Distros to test. Order = small → large.
# ubuntu:25.10 (questing) added 5/14 per Yongtao P0 T-20260514-150707 —
# bootstrap.sh must work on ANY Ubuntu release including dev releases.
# questing's deadsnakes PPA returns 404; bootstrap.sh Attempt 2
# (system python3 ≥ 3.12 symlinked as python3.12) handles it.
DISTROS=(
    "ubuntu:22.04"
    "ubuntu:24.04"
    "ubuntu:25.10"
    "debian:12"
)

# Negative test distro — should fail OS check.
UNSUPPORTED_DISTROS=(
    "rockylinux:9"
)

PASS=0
FAIL=0

emit_pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
emit_fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }


# Spin a container with the script copied in. Returns the container
# name on stdout. The caller is responsible for `docker rm -f` cleanup.
spin_container() {
    local distro="$1"
    local name; name="bootstrap-test-$(echo "$distro" | tr ':/' '--')"
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker run -d --name "$name" "$distro" sleep infinity >/dev/null
    docker cp "$BOOTSTRAP_SH" "$name:/tmp/bootstrap.sh"
    docker exec "$name" chmod +x /tmp/bootstrap.sh
    echo "$name"
}


# ── Test 1: syntax sanity (bash -n) across 3 supported distros ─────────

test_syntax_clean() {
    echo "=== Test 1: bash -n syntax sanity ==="
    for distro in "${DISTROS[@]}"; do
        local name
        name=$(spin_container "$distro")
        if docker exec "$name" bash -n /tmp/bootstrap.sh >/dev/null 2>&1; then
            emit_pass "$distro: bash -n exit 0"
        else
            emit_fail "$distro: bash -n exit != 0"
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
}


# ── Test 2: --help head + exit 0 ───────────────────────────────────────

test_help() {
    echo "=== Test 2: --help head ==="
    for distro in "${DISTROS[@]}"; do
        local name
        name=$(spin_container "$distro")
        local out="$LOG_DIR/help-$distro.txt"
        if docker exec "$name" bash /tmp/bootstrap.sh --help >"$out" 2>&1; then
            if grep -q "bootstrap.sh" "$out" && grep -q "Invocation" "$out"; then
                emit_pass "$distro: --help prints header"
            else
                emit_fail "$distro: --help output missing expected lines"
            fi
        else
            emit_fail "$distro: --help exit != 0"
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
}


# ── Test 3: bad-flag → exit 2 + STATUS: failure: bad_args ──────────────
#
# Also verifies the LineStreamer parser-clean stdout contract: both the
# STEP fail marker and the terminal STATUS line MUST land on stdout, NOT
# stderr (the Tauri russh wire-impl currently drops stderr chunks; a
# stderr-routed STEP would never reach the SetupWizard step-chip UI).
# This test was tightened 5/12 after Bootstrap Dev LineStreamer contract
# validation audit caught a stderr-routed STEP line on the bad-args path.

test_bad_flag() {
    echo "=== Test 3: --bogus → exit 2 + bad_args + stdout-clean ==="
    for distro in "${DISTROS[@]}"; do
        local name
        name=$(spin_container "$distro")
        local out_stdout="$LOG_DIR/badflag-$distro.stdout.txt"
        local out_stderr="$LOG_DIR/badflag-$distro.stderr.txt"
        # Separate stdout vs stderr so we can assert the parser-clean
        # contract: STEP/STATUS/RESULT_* land on stdout only.
        # Set +e so the failing exec doesn't kill our test driver.
        set +e
        docker exec "$name" bash /tmp/bootstrap.sh --bogus \
            >"$out_stdout" 2>"$out_stderr"
        local rc=$?
        set -e
        if [ "$rc" -eq 2 ] \
            && grep -q "STEP: arg-parse ✗" "$out_stdout" \
            && grep -q "STATUS: failure: bad_args" "$out_stdout" \
            && ! grep -q "STEP: arg-parse ✗" "$out_stderr" \
            && ! grep -q "STATUS: failure: bad_args" "$out_stderr"; then
            emit_pass "$distro: bad-flag exit 2 + STEP+STATUS on stdout (no stderr leak)"
        else
            emit_fail "$distro: bad-flag rc=$rc, STEP/STATUS contract drift"
            echo "  stdout:" >&2
            cat "$out_stdout" >&2
            echo "  stderr:" >&2
            cat "$out_stderr" >&2
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
}


# ── Test 4: --dry-run probe path on supported distros ──────────────────
#
# Probes idempotency + OS check + tooling probe. Skips full install
# (which needs systemd, out of v1 Docker scope). Containers may lack
# `sudo` and `git` by default — install via apt before invoking, since
# bootstrap.sh's tooling probe will hard-fail otherwise.

test_dry_run() {
    echo "=== Test 4: --dry-run probe on supported distros ==="
    for distro in "${DISTROS[@]}"; do
        local name
        name=$(spin_container "$distro")
        # Pre-install tooling so the tooling-probe step passes. This
        # mirrors the pre-existing-VPS-state assumption that real
        # Mac-app-fired bootstrap runs against.
        docker exec "$name" apt-get update -y >/dev/null 2>&1 || true
        docker exec "$name" apt-get install -y curl git openssl sudo \
            >/dev/null 2>&1 || true
        local out="$LOG_DIR/dryrun-$distro.txt"
        # Set +e — we read the exit code explicitly.
        set +e
        docker exec "$name" bash /tmp/bootstrap.sh --dry-run >"$out" 2>&1
        local rc=$?
        set -e
        # Expected: STATUS: dry_run_ok (fresh) OR STATUS: already_installed
        # (impossible in fresh container, but defense-in-depth).
        if [ "$rc" -eq 0 ] && grep -qE "STATUS: (dry_run_ok|already_installed)" "$out"; then
            emit_pass "$distro: --dry-run exit 0 + valid terminal status"
        else
            emit_fail "$distro: --dry-run rc=$rc"
            tail -20 "$out" >&2
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
}


# ── Test 6: python-install fallback succeeds on each supported Ubuntu ──
#
# Verifies bootstrap.sh `install_python_if_needed` reaches its success
# emit on all 3 Ubuntu distros incl. ubuntu:25.10 questing (Yongtao 5/14
# P0 T-20260514-150707 — non-LTS support). Bootstrap will die LATER at
# enable-linger (no systemd in plain containers), but `STEP:
# python-install ✓` must appear BEFORE the failure — that's our gate.
#
# Expected fallback paths per distro:
#   ubuntu:22.04 → Attempt 3 (deadsnakes PPA; system python3=3.10 < 3.12)
#   ubuntu:24.04 → Attempt 1 (main repos; python3.12 in 24.04 main)
#   ubuntu:25.10 → Attempt 2 (system python3=3.13 symlinked as python3.12;
#                  deadsnakes PPA returns 404 on questing)
#
# debian:12 is excluded here — bootstrap.sh intentionally dies at
# python-install on Debian (debian_needs_python312_manual) per CTO spec
# §13 + L2 CLAUDE.md "Ubuntu 22.04+ only" rejection. Existing tests 1-4
# still cover debian:12 syntax/help/bogus/dry-run.

PYINSTALL_DISTROS=(
    "ubuntu:22.04"
    "ubuntu:24.04"
    "ubuntu:25.10"
)

test_python_install() {
    echo "=== Test 6: python-install fallback (Yongtao 5/14 non-LTS support) ==="
    for distro in "${PYINSTALL_DISTROS[@]}"; do
        local name
        name=$(spin_container "$distro")
        # Pre-install tooling. software-properties-common is needed by
        # bootstrap.sh Attempt 3 (deadsnakes) — provides add-apt-repository.
        docker exec "$name" apt-get update -y >/dev/null 2>&1 || true
        docker exec "$name" apt-get install -y curl git openssl sudo software-properties-common \
            >/dev/null 2>&1 || true
        local out="$LOG_DIR/python-install-${distro//[:.]/_}.txt"
        # Set +e — we don't gate on overall exit (script will die at
        # enable-linger downstream; we just check the python-install
        # step marker.)
        set +e
        docker exec "$name" bash /tmp/bootstrap.sh > "$out" 2>&1
        set -e
        if grep -q "^STEP: python-install ✓" "$out"; then
            local detail
            detail=$(grep "^STEP: python-install ✓" "$out" | head -1 | sed 's|^STEP: python-install ✓ *||')
            emit_pass "$distro: python-install ✓ ($detail)"
        else
            emit_fail "$distro: python-install did not reach ✓"
            grep -E "^STEP:|^STATUS:" "$out" >&2 | head -10 || true
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
}


# ── Test 5: unsupported OS → STATUS: failure: unsupported_os ───────────

test_unsupported_os() {
    echo "=== Test 5: unsupported OS → failure ==="
    for distro in "${UNSUPPORTED_DISTROS[@]}"; do
        local name
        name=$(spin_container "$distro")
        # RHEL family uses yum/dnf; bash + bash-syntax should work but
        # OS-check should reject.
        local out="$LOG_DIR/unsupported-$distro.txt"
        set +e
        docker exec "$name" bash /tmp/bootstrap.sh >"$out" 2>&1
        local rc=$?
        set -e
        if [ "$rc" -ne 0 ] && grep -q "STATUS: failure: unsupported_os" "$out"; then
            emit_pass "$distro: rejected with unsupported_os"
        else
            emit_fail "$distro: unexpected rc=$rc or status"
            tail -10 "$out" >&2
        fi
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
}


# ── Driver ────────────────────────────────────────────────────────────

main() {
    echo "bootstrap.sh integration tests"
    echo "  script:  $BOOTSTRAP_SH"
    echo "  logs:    $LOG_DIR"
    echo ""

    test_syntax_clean
    test_help
    test_bad_flag
    test_dry_run
    test_python_install
    test_unsupported_os

    echo ""
    echo "============================================="
    echo "  pass: $PASS"
    echo "  fail: $FAIL"
    echo "============================================="
    if [ "$FAIL" -gt 0 ]; then
        echo "logs preserved at $LOG_DIR"
        exit 1
    fi
    rm -rf "$LOG_DIR"
    exit 0
}


main "$@"

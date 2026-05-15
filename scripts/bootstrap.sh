#!/usr/bin/env bash
# bootstrap.sh — NODEBLE api-server first-install for Path C Model A flow.
#
# Invocation (typical, via Tauri Phase F' russh client):
#
#   curl -fsSL https://raw.githubusercontent.com/Holycrabeth/nodeble-api-server/main/scripts/bootstrap.sh \
#     | bash
#
# Or with override env vars:
#
#   NODEBLE_HOSTNAME=my.host.com bash bootstrap.sh
#   NODEBLE_API_PORT=9876        bash bootstrap.sh
#
# Flags:
#   --verbose   Stream all subprocess output to stdout (human-debug mode).
#               Default OFF — Tauri parser-clean mode emits only
#               STEP/STATUS/RESULT_* lines to stdout.
#   --dry-run   Probe-only: runs Step 0 + Step 1 idempotency + OS check,
#               skips all side-effecting operations. Exits STATUS: dry_run_ok.
#
# Output contract (parsed by Tauri Phase F' russh stream — see
# nodeble-desktop/src-tauri/src/ssh/stream.rs):
#
#   STEP: <step-id>                   step running
#   STEP: <step-id> ✓ [<detail>]      step ok
#   STEP: <step-id> ✗ <reason>        step fail
#   RESULT_<KEY>: <value>             captured value (BEARER_TOKEN /
#                                                    FINGERPRINT / PORT)
#   STATUS: success                   terminal success
#   STATUS: already_installed         terminal — existing install reused
#   STATUS: failure: <reason>         terminal failure (exit 1)
#   STATUS: dry_run_ok                terminal — --dry-run smoke
#                                       (developer/CI only; NOT consumed
#                                       by Tauri parser, which invokes
#                                       bootstrap.sh WITHOUT --dry-run.
#                                       Parser's parse_status_payload
#                                       defensive-treats unknown STATUS
#                                       payloads as Failure; dry_run_ok
#                                       only reached via local invocation
#                                       e.g. tests/integration/test_bootstrap.sh.)
#
# Spec source: ~/projects/cto/reviews/2026-05-05-bootstrap-sh-design.md
# (ratified 5/5 with A1-A5 amendments; verify-from-source 5/11).
#
# Path C Phase F''' (5/11 kickoff per 协作总监 dispatch) — api-server
# install only. Chain installs (orchestrator + allocator) are Bootstrap
# Dev's separate `nodeble-web/bootstrap.sh` workstream (different repo,
# different hosting). Don't confuse the two.
#
# Token field convention (NEW 5/11): `RESULT_BEARER_TOKEN: <UUID>` is the
# canonical line. Legacy `RESULT_TOKEN` retained as additional emit until
# Bootstrap Dev rename PR fully propagates across all consumers — at
# which point this script drops the legacy alias.

set -o errexit
set -o nounset
set -o pipefail

# Suppress apt interactive prompts (tzdata, libc6 service restart, etc.)
# globally. Without this, apt-get install on minimal cloud images / Docker
# containers hangs waiting for tty input that never arrives via SSH exec.
# Surfaced 5/14 by Test 6 deadsnakes-path stall on ubuntu:22.04 CI runner
# — Phase F''' v1 (--dry-run only) didn't exercise apt install of
# python3.12 deps so this latent bug was masked.
export DEBIAN_FRONTEND=noninteractive

# ── Config ─────────────────────────────────────────────────────────────

# $USER is unset under non-interactive `docker exec` / some SSH exec
# configs (no login shell to populate it). `set -o nounset` would then
# hard-exit (no die(), no STATUS line) the moment enable_linger
# references it. Resolve once with an id(1) fallback. Mirrors
# nodeble-web/bootstrap.sh USER_NAME pattern. Surfaced 5/15 by test 7
# (first full-bootstrap-past-enable-linger coverage on api-server).
USER_NAME="${USER:-$(id -un)}"

REPO_URL="https://github.com/Holycrabeth/nodeble-api-server.git"
REPO_DIR="$HOME/projects/nodeble-api-server"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
CONFIG_DIR="$HOME/.nodeble-api/config"
CONFIG_YAML="$CONFIG_DIR/api.yaml"
TLS_DIR="$HOME/.nodeble-api/tls"
SVC_DIR="$HOME/.config/systemd/user"
SVC_FILE="$SVC_DIR/nodeble-api-server.service"
API_PORT="${NODEBLE_API_PORT:-8765}"

# Per-step state (set by emit_* / consumed by die).
CURRENT_STEP=""
BOOTSTRAP_TOKEN=""
BOOTSTRAP_FINGERPRINT=""

# CLI flags
VERBOSE=0
DRY_RUN=0


# ── Argument parsing ───────────────────────────────────────────────────

while [ $# -gt 0 ]; do
    case "$1" in
        --verbose)
            VERBOSE=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            # STEP line MUST go to stdout (not stderr) — parser-clean
            # stdout contract per header lines 21-32 + stream.rs
            # LineStreamer wire grammar. Tauri russh wire-impl (Phase F'
            # chunk 3+) routes stdout chunks through LineStreamer::push;
            # stderr chunks are reserved for future ExtendedData routing
            # and currently get dropped, so a stderr-routed STEP line
            # would never reach the SetupWizard step-chip UI.
            echo "STEP: arg-parse ✗ unknown flag: $1"
            echo "STATUS: failure: bad_args"
            exit 2
            ;;
    esac
done


# ── Stdout helpers ─────────────────────────────────────────────────────
#
# All STEP/STATUS/RESULT_* lines go to STDOUT for Tauri parser.
# All subprocess output goes to STDERR (or stdout if --verbose).

emit_step()      { CURRENT_STEP="$1"; echo "STEP: $1"; }
emit_step_ok()   { echo "STEP: $1 ✓${2:+ $2}"; }
emit_step_fail() { echo "STEP: $1 ✗ $2"; }
emit_status()    { echo "STATUS: $1"; }
emit_result()    { echo "RESULT_$1: $2"; }

# Run a subprocess; route output per --verbose flag.
quiet() {
    if [ "$VERBOSE" -eq 1 ]; then
        "$@"
    else
        "$@" >&2 2>&1
    fi
}

# Terminal failure helper. Emit STEP fail (using current step) + STATUS
# failure + exit 1. Caller passes a short reason key (snake_case) so the
# Tauri parser can drop it into the §6 state machine error category.
die() {
    local reason="$1"
    if [ -n "$CURRENT_STEP" ]; then
        emit_step_fail "$CURRENT_STEP" "$reason"
    fi
    emit_status "failure: $reason"
    exit 1
}


# ── Step 0 — Idempotency probe (spec §3.1) ──────────────────────────────
#
# If api-server already installed + running + readable config exists,
# extract existing token/fingerprint/port + emit STATUS: already_installed
# and exit 0. Tauri reuses these without re-installing.

probe_existing_install() {
    [ -x "$VENV_PYTHON" ] || return 1
    [ -r "$CONFIG_YAML" ] || return 1
    systemctl --user is-active nodeble-api-server.service >/dev/null 2>&1 || return 1
    return 0
}

# Extract a field from the existing api.yaml (poor man's YAML reader —
# good enough for the keys we wrote in Step 7). Falls back to empty
# string if not found so caller can route to a defensive failure.
yaml_get() {
    local key="$1"
    local path="$2"
    # Match "  <key>: <value>" anywhere in the file. The space-prefix
    # filters top-level keys with the same name in adjacent sections.
    grep -E "^\s*${key}: " "$path" 2>/dev/null | head -n 1 | sed -E "s/^\s*${key}: //" | tr -d '"'
}

emit_existing_result_lines() {
    # api.yaml's `auth.valid_tokens[]` list — the first token entry is
    # the bootstrap-initial token by convention.
    local existing_token
    existing_token=$(grep -E "^\s+- token: " "$CONFIG_YAML" 2>/dev/null \
                     | head -n 1 \
                     | sed -E 's/^\s+- token: //' \
                     | tr -d '"')
    local existing_fp
    existing_fp=$(yaml_get "fingerprint" "$CONFIG_YAML")
    local existing_port
    existing_port=$(yaml_get "port" "$CONFIG_YAML")
    if [ -z "$existing_port" ]; then
        existing_port="$API_PORT"
    fi
    # Emit canonical BEARER_TOKEN AND legacy TOKEN alias for back-compat.
    emit_result "BEARER_TOKEN" "$existing_token"
    emit_result "TOKEN" "$existing_token"
    emit_result "FINGERPRINT" "$existing_fp"
    emit_result "PORT" "$existing_port"
}


# ── Step 1 — OS check (spec §3.2) ──────────────────────────────────────

check_os() {
    # /etc/os-release is POSIX-standard on systemd boxes (which is every
    # Ubuntu/Debian we support per spec §13).
    if [ ! -r /etc/os-release ]; then
        die "no_os_release"
    fi
    # shellcheck source=/dev/null
    . /etc/os-release
    case "${ID:-unknown}" in
        ubuntu)
            local major
            major=$(echo "${VERSION_ID:-0}" | cut -d. -f1)
            if [ "$major" -lt 22 ] 2>/dev/null; then
                die "ubuntu_too_old"
            fi
            emit_step_ok "os-check" "Ubuntu ${VERSION_ID} detected"
            ;;
        debian)
            local dmajor="${VERSION_ID%%.*}"
            if [ "$dmajor" -lt 12 ] 2>/dev/null; then
                die "debian_too_old"
            fi
            emit_step_ok "os-check" "Debian ${VERSION_ID} detected"
            ;;
        *)
            die "unsupported_os"
            ;;
    esac
}


# ── Step 1.5 — Sudo + tooling probe (spec §11 open question #1) ────────
#
# Bootstrap needs `sudo` for apt-get + add-apt-repository. If the SSH
# user is a non-sudoer (rare on fresh VPSes), fail early with a clear
# reason rather than hanging on a non-interactive password prompt.

probe_sudo_and_tooling() {
    # `sudo -n true` returns 0 if NOPASSWD configured, 1 otherwise (or
    # 1 if sudo isn't installed at all). Root user doesn't need sudo.
    if [ "$(id -u)" -ne 0 ]; then
        if ! sudo -n true 2>/dev/null; then
            die "requires_sudo_nopasswd"
        fi
    fi
    # Required tooling. curl + git are typical SSH-image defaults on
    # major VPS providers; verify presence so failure is in the probe
    # rather than 3 steps deep in a tangle.
    for tool in curl git openssl; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            die "missing_tool: $tool"
        fi
    done
    emit_step_ok "tooling-probe" "sudo + curl + git + openssl present"
}


# ── Step 2 — Python 3.12+ (spec §3.3 + Yongtao 5/14 P0 T-20260514-150707) ──
#
# 3-attempt fallback chain so bootstrap.sh works on ANY Ubuntu release —
# including dev releases like 25.10 questing where the deadsnakes PPA
# returns 404. Yongtao 5/14 directive: "我们不应该随随便便就告诉别人 vps 是
# 选什么版本的。既然有最新的为什么不用最新的。"
#   Attempt 1 — apt-get install python3.12 from main repos (Ubuntu 24.04+
#               ships python3.12 in main; fastest path).
#   Attempt 2 — system python3 ≥ 3.12 symlinked as python3.12 (non-LTS
#               releases like 25.10 questing ship newer Python natively
#               but deadsnakes PPA doesn't cover them).
#   Attempt 3 — deadsnakes PPA (Ubuntu 22.04 LTS backstop where system
#               python3 is 3.10).

install_python_if_needed() {
    # Fast path: python3.12 binary already on system (user pre-installed,
    # or re-run after prior bootstrap).
    if command -v python3.12 >/dev/null 2>&1; then
        local v
        v=$(python3.12 --version 2>&1 | awk '{print $2}')
        emit_step_ok "python-install" "Python $v already present"
        return 0
    fi

    if [ "${ID:-}" = "debian" ]; then
        # Debian 12 ships python3.11; 3.12 needs build-from-source or
        # the Debian 13 stable backport. For v1 we require 3.12 already
        # present on Debian — surface a clear error.
        die "debian_needs_python312_manual"
    fi
    if [ "${ID:-}" != "ubuntu" ]; then
        die "unsupported_os_for_python_install: ${ID:-unknown}"
    fi

    local sudo_cmd=""
    [ "$(id -u)" -ne 0 ] && sudo_cmd="sudo"

    quiet $sudo_cmd apt-get update -y 2>/dev/null || true

    # Attempt 1: apt-get install from main repos.
    if quiet $sudo_cmd apt-get install -y python3.12 python3.12-venv python3.12-dev 2>/dev/null; then
        if command -v python3.12 >/dev/null 2>&1; then
            local v
            v=$(python3.12 --version 2>&1 | awk '{print $2}')
            emit_step_ok "python-install" "Python $v installed from main repos"
            return 0
        fi
    fi

    # Attempt 2: system python3 ≥ 3.12 → symlink as python3.12.
    if command -v python3 >/dev/null 2>&1; then
        local sys_full sys_major sys_minor
        sys_full=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "")
        if [ -n "$sys_full" ]; then
            sys_major="${sys_full%%.*}"
            sys_minor="${sys_full##*.}"
            if [ "$sys_major" = "3" ] && [ "$sys_minor" -ge 12 ] 2>/dev/null; then
                # Install venv + dev modules for system python3 (separate
                # packages on Ubuntu; needed for `python -m venv` and
                # C-extension builds). `python3-venv` is a meta-package that
                # tracks the system python3 major version.
                quiet $sudo_cmd apt-get install -y python3-venv python3-dev \
                    || die "python3_venv_install_failed"
                # Symlink system python3 → /usr/local/bin/python3.12 so the
                # rest of bootstrap.sh (venv creation etc.) which hardcodes
                # python3.12 resolves to the system 3.12+ binary.
                local py3_path
                py3_path=$(command -v python3)
                $sudo_cmd mkdir -p /usr/local/bin
                $sudo_cmd ln -sf "$py3_path" /usr/local/bin/python3.12 \
                    || die "python3_12_symlink_failed"
                emit_step_ok "python-install" "Python $sys_full (system) symlinked as python3.12 (non-LTS fallback)"
                return 0
            fi
        fi
    fi

    # Attempt 3: deadsnakes PPA (Ubuntu 22.04 LTS backstop).
    # software-properties-common provides add-apt-repository on minimal
    # container/cloud images that may not pre-install it.
    quiet $sudo_cmd apt-get install -y software-properties-common \
        || die "software_properties_install_failed"
    quiet $sudo_cmd add-apt-repository -y ppa:deadsnakes/ppa \
        || die "deadsnakes_ppa_failed"
    quiet $sudo_cmd apt-get update -y \
        || die "apt_update_failed"
    quiet $sudo_cmd apt-get install -y python3.12 python3.12-venv python3.12-dev \
        || die "python_install_failed"
    if ! command -v python3.12 >/dev/null 2>&1; then
        die "python_install_failed"
    fi
    emit_step_ok "python-install" "Python 3.12 installed via deadsnakes PPA"
}


# ── Step 3 — loginctl enable-linger (spec §3 + §12 risk #2) ────────────

enable_linger() {
    # loginctl enable-linger $USER_NAME is itself idempotent; running it
    # repeatedly is a no-op. But it requires sudo on most distros.
    local sudo_cmd=""
    [ "$(id -u)" -ne 0 ] && sudo_cmd="sudo"
    quiet $sudo_cmd loginctl enable-linger "$USER_NAME" \
        || die "linger_enable_failed"
    # Verify (per spec §12 risk #2).
    if ! loginctl show-user "$USER_NAME" 2>/dev/null | grep -q "Linger=yes"; then
        die "linger_verify_failed"
    fi
    emit_step_ok "enable-linger" "user services persist after logout"
}


# ── Step 4 — Clone repo (spec §3 + §7.2 network-failure recovery) ──────

clone_repo() {
    if [ -d "$REPO_DIR/.git" ]; then
        # Existing repo: fetch + reset to origin/main for "ensure
        # latest" semantics on re-run (idempotency requirement §6).
        ( cd "$REPO_DIR" && quiet git fetch origin && quiet git reset --hard origin/main ) \
            || die "git_update_failed"
        local head
        head=$( cd "$REPO_DIR" && git log -1 --format='%h' )
        emit_step_ok "clone-repo" "updated to $head"
    else
        mkdir -p "$HOME/projects"
        ( cd "$HOME/projects" && quiet git clone "$REPO_URL" ) \
            || die "git_clone_failed"
        local head
        head=$( cd "$REPO_DIR" && git log -1 --format='%h' )
        emit_step_ok "clone-repo" "cloned at $head"
    fi
}


# ── Step 5 — venv + pip install (spec §3 + §6 idempotency) ─────────────

create_venv_and_install() {
    if [ ! -x "$VENV_PYTHON" ]; then
        ( cd "$REPO_DIR" && quiet python3.12 -m venv .venv ) \
            || die "venv_create_failed"
        emit_step_ok "venv-create" ".venv created"
    else
        emit_step_ok "venv-create" ".venv already present"
    fi
    # pip install -e . is itself idempotent.
    emit_step "pip-install"
    ( cd "$REPO_DIR" && quiet "$VENV_PYTHON" -m pip install --upgrade pip ) \
        || die "pip_upgrade_failed"
    ( cd "$REPO_DIR" && quiet "$VENV_PYTHON" -m pip install -e . ) \
        || die "pip_install_failed"
    emit_step_ok "pip-install" "dependencies installed"
}


# ── Step 6 — API token (spec §3 + A3 amendment) ────────────────────────

generate_api_token() {
    if [ -z "$BOOTSTRAP_TOKEN" ]; then
        BOOTSTRAP_TOKEN=$("$VENV_PYTHON" -c 'import uuid; print(uuid.uuid4())' 2>/dev/null) \
            || die "token_gen_failed"
        if [ -z "$BOOTSTRAP_TOKEN" ]; then
            die "token_gen_empty"
        fi
    fi
    emit_step_ok "generate-api-token" "uuid4 generated"
}


# ── Step 7 — TLS cert with SAN (spec §3.4 + A2 amendment) ──────────────

generate_tls_cert() {
    mkdir -p "$TLS_DIR"
    chmod 700 "$TLS_DIR"

    # Idempotency per §6: skip cert gen if existing cert is still valid.
    # Use OpenSSL to check the expiry date; if -checkend exits 0 with
    # 24h grace, the cert is fresh enough.
    if [ -f "$TLS_DIR/cert.pem" ] && [ -f "$TLS_DIR/key.pem" ]; then
        if openssl x509 -in "$TLS_DIR/cert.pem" -noout -checkend 86400 >/dev/null 2>&1; then
            BOOTSTRAP_FINGERPRINT=$(openssl x509 -in "$TLS_DIR/cert.pem" -noout -fingerprint -sha256 \
                                    | cut -d= -f2)
            emit_step_ok "generate-tls-cert" "reusing existing cert (still valid)"
            return 0
        fi
    fi

    # Auto-detect public IP with 3-service fallback (spec §11 #2). Each
    # call timeouts at 5s so total worst-case = 15s. If all fail, the
    # cert just lacks the public-IP SAN — localhost/127.0.0.1 paths
    # still work for Tauri loopback testing.
    local public_ip=""
    for svc in ifconfig.me icanhazip.com ipify.org; do
        public_ip=$(curl -fsSL --max-time 5 "https://$svc" 2>/dev/null || echo "")
        if [ -n "$public_ip" ]; then
            break
        fi
    done

    # Build SAN list.
    local san="DNS:localhost,IP:127.0.0.1"
    if [ -n "$public_ip" ]; then
        san="$san,IP:$public_ip"
    fi
    if [ -n "${NODEBLE_HOSTNAME:-}" ]; then
        san="$san,DNS:$NODEBLE_HOSTNAME"
    fi

    # Generate self-signed cert. 825-day validity = Apple's cap for
    # trusted certs (irrelevant for Tauri TLS pinning but conservative).
    quiet openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
        -keyout "$TLS_DIR/key.pem" \
        -out "$TLS_DIR/cert.pem" \
        -subj "/CN=NODEBLE-API-SERVER" \
        -addext "subjectAltName=$san" \
        || die "cert_generation_failed"

    chmod 600 "$TLS_DIR"/*.pem

    BOOTSTRAP_FINGERPRINT=$(openssl x509 -in "$TLS_DIR/cert.pem" -noout -fingerprint -sha256 \
                            | cut -d= -f2)
    echo "$BOOTSTRAP_FINGERPRINT" > "$TLS_DIR/fingerprint.txt"

    emit_step_ok "generate-tls-cert" "SAN: $san"
}


# ── Step 8 — Write api.yaml (spec §3.5 / A3 amendment) ─────────────────

write_api_yaml() {
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"

    # Idempotency per §6: NEVER overwrite an existing api.yaml. If a
    # customer hand-edited config exists, we read its existing token
    # and don't issue a new one (preserves Mac-app pairing).
    if [ -r "$CONFIG_YAML" ]; then
        local existing_token
        existing_token=$(grep -E "^\s+- token: " "$CONFIG_YAML" 2>/dev/null \
                         | head -n 1 | sed -E 's/^\s+- token: //' | tr -d '"')
        if [ -n "$existing_token" ]; then
            BOOTSTRAP_TOKEN="$existing_token"
            emit_step_ok "write-api-yaml" "existing config preserved"
            return 0
        fi
    fi

    # Write fresh config.
    cat > "$CONFIG_YAML" <<EOF
# Generated by bootstrap.sh on $(date -Iseconds)
server:
  host: 0.0.0.0
  port: $API_PORT
auth:
  valid_tokens:
    - token: $BOOTSTRAP_TOKEN
      label: bootstrap-initial
tls:
  cert_path: $TLS_DIR/cert.pem
  key_path: $TLS_DIR/key.pem
  fingerprint: $BOOTSTRAP_FINGERPRINT
EOF
    chmod 600 "$CONFIG_YAML"
    emit_step_ok "write-api-yaml" "config + token written"
}


# ── Step 9 — systemd USER service install (spec §3.6) ──────────────────

install_systemd_service() {
    mkdir -p "$SVC_DIR"

    # Write unit file. `%h` = systemd's home-dir variable so the unit
    # is user-account-portable.
    cat > "$SVC_FILE" <<'EOF'
[Unit]
Description=NODEBLE API Server (FastAPI sidecar for desktop app)
Documentation=https://github.com/Holycrabeth/nodeble-api-server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/projects/nodeble-api-server
ExecStart=%h/projects/nodeble-api-server/.venv/bin/python -m nodeble_api_server
Restart=always
RestartSec=5
TimeoutStopSec=15
TimeoutStartSec=30
SuccessExitStatus=0 143
KillMode=control-group
KillSignal=SIGTERM

[Install]
WantedBy=default.target
EOF

    quiet systemctl --user daemon-reload || die "daemon_reload_failed"
    quiet systemctl --user enable nodeble-api-server.service || die "enable_failed"
    emit_step_ok "systemd-install" "unit installed + enabled"

    emit_step "systemd-start"
    # Restart (not just start) — handles the case where a stale unit
    # was running with old config. Restart is idempotent w/ start.
    quiet systemctl --user restart nodeble-api-server.service || die "systemd_start_failed"

    # Wait up to 10s for the service to bind its port. If it doesn't,
    # surface a timeout — operator can inspect via Settings → Diagnostics.
    local pid=""
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ss -tlnp 2>/dev/null | grep -q ":${API_PORT} "; then
            pid=$(systemctl --user show -p MainPID nodeble-api-server.service 2>/dev/null \
                  | cut -d= -f2)
            emit_step_ok "systemd-start" "PID ${pid:-?}, listening on 0.0.0.0:${API_PORT}"
            return 0
        fi
        sleep 1
    done
    die "systemd_start_timeout"
}


# ── Step 9.5 — Open firewall for API port (P0 T-20260515-210530) ───────
#
# Vultr + most cloud VMs ship `ufw` active allowing only 22/tcp. The
# api-server binds 0.0.0.0:${API_PORT} and is locally healthy, but the
# Mac app (a public client) cannot reach ${API_PORT} until the host
# firewall allows it. 前端总监 verify-from-source 2026-05-15 confirmed
# this is the root cause of the destroy-rebuild-same-problem loop —
# SetupWizard writes connection.json, then RootRedirect's
# probeEndpointAlive(https://IP:8765/health) times out → setDeadHost →
# bounce back to /setup, despite fingerprint/token/SSH-fp all matching.
#
# Idempotent across ufw / firewalld / pure-iptables. die loud on real
# failure: an install whose port stays blocked is the exact silent-
# success bug we're fixing — emitting STATUS: success with an
# unreachable server is worse than failing.

open_firewall_port() {
    local sudo_cmd=""
    [ "$(id -u)" -ne 0 ] && sudo_cmd="sudo"
    local port="$API_PORT"

    # 1. ufw (Ubuntu/Vultr default). `ufw allow` is idempotent — re-adding
    #    an existing rule prints "Skipping adding existing rule", exit 0.
    #    `grep -qw active` distinguishes "Status: active" from "inactive"
    #    (word-boundary: "active" in "inactive" is not a whole word).
    if command -v ufw >/dev/null 2>&1 \
       && $sudo_cmd ufw status 2>/dev/null | grep -qw active; then
        $sudo_cmd ufw allow "${port}/tcp" >/dev/null 2>&1 \
            || die "ufw_allow_failed"
        $sudo_cmd ufw reload >/dev/null 2>&1 || true
        emit_step_ok "firewall-open" "ufw allow ${port}/tcp"
        return 0
    fi

    # 2. firewalld (RHEL-family; rare since os-check gates Ubuntu/Debian,
    #    kept for defense + future OS support). --add-port is idempotent
    #    (ALREADY_ENABLED warning still exits 0).
    if command -v firewall-cmd >/dev/null 2>&1 \
       && $sudo_cmd firewall-cmd --state >/dev/null 2>&1; then
        $sudo_cmd firewall-cmd --add-port="${port}/tcp" --permanent >/dev/null 2>&1 \
            || die "firewalld_add_port_failed"
        $sudo_cmd firewall-cmd --reload >/dev/null 2>&1 || true
        emit_step_ok "firewall-open" "firewalld add-port ${port}/tcp"
        return 0
    fi

    # 3. Pure-iptables fallback: only act if INPUT policy is DROP/REJECT
    #    (otherwise the port is already reachable; adding a rule is noise).
    #    `-C` check-then-`-I` insert is idempotent.
    if command -v iptables >/dev/null 2>&1; then
        local input_policy
        input_policy=$($sudo_cmd iptables -S INPUT 2>/dev/null | head -1)
        if printf '%s' "$input_policy" | grep -qE -- '-P INPUT (DROP|REJECT)'; then
            if ! $sudo_cmd iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
                $sudo_cmd iptables -I INPUT -p tcp --dport "$port" -j ACCEPT \
                    || die "iptables_insert_failed"
            fi
            # Persist across reboot if the tooling is present (best-effort).
            if command -v netfilter-persistent >/dev/null 2>&1; then
                $sudo_cmd netfilter-persistent save >/dev/null 2>&1 || true
            elif command -v iptables-save >/dev/null 2>&1 && [ -d /etc/iptables ]; then
                $sudo_cmd sh -c 'iptables-save > /etc/iptables/rules.v4' 2>/dev/null || true
            fi
            emit_step_ok "firewall-open" "iptables ACCEPT tcp dpt ${port}"
            return 0
        fi
    fi

    # 4. No active blocking firewall detected — port already reachable.
    emit_step_ok "firewall-open" "no active firewall (port ${port} already reachable)"
}


# ── Step 10 — RESULT lines + STATUS terminal (spec §2 + A4) ────────────

emit_results_and_finish() {
    # Canonical (post Bootstrap Dev 5/11 rename) + legacy alias for the
    # in-flight transition. Both lines carry the same UUID; Tauri parser
    # `captured_bearer_token()` prefers canonical when both present
    # (see nodeble-desktop/src-tauri/src/ssh/stream.rs).
    emit_result "BEARER_TOKEN" "$BOOTSTRAP_TOKEN"
    emit_result "TOKEN"        "$BOOTSTRAP_TOKEN"
    emit_result "FINGERPRINT"  "$BOOTSTRAP_FINGERPRINT"
    emit_result "PORT"         "$API_PORT"
    emit_status "success"
}


# ── Main orchestrator ─────────────────────────────────────────────────

main() {
    # `systemctl --user` (idempotency-probe Step 0 + systemd-install
    # Step 9) needs XDG_RUNTIME_DIR. PAM (pam_systemd) sets it in
    # interactive login sessions; non-interactive `docker exec` / some
    # SSH exec configs do NOT. Set it proactively BEFORE the
    # idempotency probe so a re-run correctly detects already_installed
    # instead of bus-error → false "fresh install" → full re-install.
    # On a truly fresh box /run/user/<uid> doesn't exist yet, so
    # systemctl --user fails gracefully → probe returns 1 → fresh path
    # (correct). On already-installed boxes the dir persists from prior
    # linger. Mirrors nodeble-web/bootstrap.sh. Surfaced 5/15 by test 7
    # (first full-bootstrap-past-enable-linger coverage on api-server).
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

    # Step 0 — idempotency probe.
    emit_step "idempotency-probe"
    if probe_existing_install; then
        emit_step_ok "idempotency-probe" "api-server already installed + running"
        emit_existing_result_lines
        emit_status "already_installed"
        exit 0
    fi
    emit_step_ok "idempotency-probe" "fresh install (no existing setup)"

    # Step 1 — OS check (read-only, always safe).
    emit_step "os-check"
    check_os

    # --dry-run short-circuit: probe OS + sudo only, skip side effects.
    if [ "$DRY_RUN" -eq 1 ]; then
        emit_step "tooling-probe"
        probe_sudo_and_tooling
        emit_status "dry_run_ok"
        exit 0
    fi

    # Step 1.5 — sudo / required-tool probe.
    emit_step "tooling-probe"
    probe_sudo_and_tooling

    # Step 2 — Python 3.12.
    emit_step "python-install"
    install_python_if_needed

    # Step 3 — loginctl enable-linger.
    emit_step "enable-linger"
    enable_linger

    # Step 4 — clone repo.
    emit_step "clone-repo"
    clone_repo

    # Step 5 — venv + pip install.
    emit_step "venv-create"
    create_venv_and_install

    # Step 6 — generate API token.
    emit_step "generate-api-token"
    generate_api_token

    # Step 7 — TLS cert with SAN.
    emit_step "generate-tls-cert"
    generate_tls_cert

    # Step 8 — write api.yaml.
    emit_step "write-api-yaml"
    write_api_yaml

    # Step 9 — systemd USER service install + start.
    emit_step "systemd-install"
    install_systemd_service

    # Step 9.5 — open host firewall for the API port so the Mac app
    # (public client) can actually reach the now-listening service
    # (P0 T-20260515-210530). Placed AFTER systemd-start confirms the
    # port is bound — don't open a hole to nothing.
    emit_step "firewall-open"
    open_firewall_port

    # Step 10 — emit RESULT + STATUS terminal (A4: STATUS must be the
    # last stdout line).
    emit_results_and_finish
}


main "$@"

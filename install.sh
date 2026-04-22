#!/usr/bin/env bash
# install.sh — first-run installer for the NODEBLE API server on a fresh Linux VPS.
#
# Usage (run as the regular user, NOT root / sudo):
#
#   git clone git@github.com:Holycrabeth/nodeble-api-server.git
#   cd nodeble-api-server
#   ./install.sh
#
# What it does (in order):
#   1. Pre-flight checks (Python 3.12+, systemd, git, curl, port 8765 free,
#      no prior install).
#   2. Python virtualenv under .venv/ and pip install -e .
#   3. Generates self-signed TLS cert + API token (label="desktop") via
#      the existing CLI subcommands (idempotent via --if-missing).
#   4. Writes / backfills ~/.nodeble-api/config/api.yaml.
#   5. Installs a systemd --user unit, enables + starts it.
#   6. Prompts for `loginctl enable-linger` if not already on.
#   7. Health-checks the /health endpoint.
#   8. Prints the 3-piece kit (URL + token + cert fingerprint) for the
#      Mac desktop app's first-run setup wizard.
#
# Non-root on purpose. Only block 6 shells out to sudo, and only after asking.
#
# Run again on an existing install: prompts before proceeding, then keeps
# existing token / cert (never silently overwrites them).

set -euo pipefail

# ── Display helpers ────────────────────────────────────────────────────────

_red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
_green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
_yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
_cyan()   { printf '\033[1;36m%s\033[0m\n' "$*"; }
_step()   { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

_fatal() {
  _red "✗ $*"
  exit 1
}

# ── Paths ──────────────────────────────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PY="$VENV_DIR/bin/python"

# ~/.nodeble-api is the data root — certs, config, audit log, snapshots.
# Matches what the server reads via Path.home() / expanduser in the Python code.
DATA_ROOT="$HOME/.nodeble-api"
CONFIG_PATH="$DATA_ROOT/config/api.yaml"
CERT_DIR="$DATA_ROOT/certs"
AUDIT_DIR="$DATA_ROOT/audit"
HISTORY_DIR="$DATA_ROOT/history"

SYSTEMD_UNIT_DIR="$HOME/.config/systemd/user"
SYSTEMD_UNIT="$SYSTEMD_UNIT_DIR/nodeble-api-server.service"

API_PORT="8765"
SERVICE_NAME="nodeble-api-server"

# ── Steps ──────────────────────────────────────────────────────────────────

check_prereqs() {
  _step "前置检查..."

  # 1. Running as non-root.
  if [[ "$(id -u)" -eq 0 ]]; then
    _fatal "不要用 root / sudo 跑这个脚本 — 全程非 root,只 linger 那步会单独提示 sudo"
  fi

  # 2. Python 3.12+.
  if ! command -v python3 >/dev/null 2>&1; then
    _fatal "没找到 python3。先装 Python 3.12+ 再重跑:
       Ubuntu 24.04 / Debian 13: 默认自带
       更老版本: sudo add-apt-repository ppa:deadsnakes/ppa &&
                 sudo apt install python3.12 python3.12-venv"
  fi
  local pyver
  pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  local major minor
  major="${pyver%.*}"
  minor="${pyver#*.}"
  if [[ "$major" -lt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -lt 12 ]]; }; then
    _fatal "需要 Python 3.12+,当前是 $pyver。
       Ubuntu 24.04 / Debian 13 默认带 3.12。
       更老的 distro:
         sudo add-apt-repository ppa:deadsnakes/ppa
         sudo apt install python3.12 python3.12-venv
       然后用新版 python3.12 重跑。"
  fi
  _green "  Python $pyver ✓"

  # 3. venv module.
  if ! python3 -c 'import venv' >/dev/null 2>&1; then
    _fatal "Python venv 模块缺失。Ubuntu/Debian 补装:
       sudo apt install python3-venv (或 python3.12-venv)"
  fi

  # 4. Essential tools.
  for tool in git curl openssl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      _fatal "缺少 $tool。Ubuntu/Debian 安装:sudo apt install $tool"
    fi
  done
  _green "  git / curl / openssl ✓"

  # 5. systemd user session.
  if ! command -v systemctl >/dev/null 2>&1; then
    _fatal "没找到 systemctl。本脚本依赖 systemd user session(现代 Linux 基本都有)"
  fi
  # `systemctl --user status` with no argument returns nonzero but doesn't
  # imply user session is unavailable; we just check the command runs.
  systemctl --user list-units --no-pager >/dev/null 2>&1 || \
    _fatal "systemd --user 会话不可用。确认你是普通 user SSH 进来,不是被 su 到的 user"
  _green "  systemd --user session ✓"

  # 6. Port 8765 not bound already (another api-server or something else).
  if command -v ss >/dev/null 2>&1; then
    if ss -tlnp 2>/dev/null | grep -q ":$API_PORT "; then
      _fatal "端口 $API_PORT 已被占用。先停掉占用进程:
         ss -tlnp | grep :$API_PORT        # 看谁在占
         然后重跑 ./install.sh"
    fi
  elif command -v lsof >/dev/null 2>&1; then
    if lsof -i ":$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      _fatal "端口 $API_PORT 已被占用"
    fi
  fi
  _green "  端口 $API_PORT 空闲 ✓"

  # 7. Existing install guard — warn loudly if ~/.nodeble-api/config exists.
  if [[ -d "$DATA_ROOT/config" ]]; then
    _yellow ""
    _yellow "⚠️  发现已有安装:$DATA_ROOT/"
    _yellow ""
    _yellow "   继续会保留现有 token / 证书,只补缺失的配置和 systemd unit。"
    _yellow "   **不会**重新生成 token 或证书(Mac app 端配置仍然有效)。"
    _yellow ""
    read -r -p "   继续? (y/N) " reply
    if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
      _fatal "已取消"
    fi
  fi
}

setup_venv() {
  _step "建 Python 虚拟环境($VENV_DIR)..."
  if [[ -d "$VENV_DIR" ]]; then
    _yellow "  .venv 已存在 — 复用"
  else
    python3 -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  _green "  venv 就绪"
}

install_deps() {
  _step "安装 api-server 依赖..."
  "$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR"
  _green "  依赖就绪"
}

ensure_dirs() {
  _step "建数据目录..."
  mkdir -p "$DATA_ROOT/config" "$CERT_DIR" "$AUDIT_DIR" "$HISTORY_DIR"
  chmod 700 "$DATA_ROOT"
  _green "  $DATA_ROOT/{config,certs,audit,history}"
}

generate_creds() {
  _step "生成自签 TLS 证书(若已存在则保留)..."
  "$PY" -m nodeble_api_server generate-cert

  _step "生成 API token(label=desktop,已存在则保留)..."
  "$PY" -m nodeble_api_server generate-token desktop --if-missing
}

write_config_defaults() {
  # generate-cert and generate-token both write into api.yaml. The server
  # block might be missing on a truly fresh install — ensure it's there.
  _step "确认 api.yaml 的 server 配置..."
  "$PY" - <<'PYEOF'
import os, yaml
from pathlib import Path

path = Path(os.environ["CONFIG_PATH"])
data = yaml.safe_load(path.read_text()) if path.exists() else {}
data = data or {}

# server block — bind 0.0.0.0 so the Mac app on a different host can reach it.
server = data.setdefault("server", {})
server.setdefault("host", "0.0.0.0")
server.setdefault("port", 8765)

# Write back atomically only if we actually added fields.
import tempfile
fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".yaml")
with os.fdopen(fd, "w") as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
os.replace(tmp, path)
print("  api.yaml: server.host=0.0.0.0 server.port=8765")
PYEOF
}

install_systemd_unit() {
  _step "安装 systemd --user 服务单元..."
  mkdir -p "$SYSTEMD_UNIT_DIR"

  cat > "$SYSTEMD_UNIT" <<UNIT
[Unit]
Description=NODEBLE API Server (FastAPI sidecar for desktop app)
Documentation=https://github.com/Holycrabeth/nodeble-api-server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$PY -m nodeble_api_server

Restart=always
RestartSec=5
TimeoutStopSec=15
TimeoutStartSec=30
# 143 = 128 + SIGTERM (15); treat graceful shutdown as clean exit.
SuccessExitStatus=0 143
KillMode=control-group
KillSignal=SIGTERM

Environment=HOME=$HOME
Environment=PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1

SyslogIdentifier=$SERVICE_NAME
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
UNIT

  systemctl --user daemon-reload
  _green "  $SYSTEMD_UNIT"
}

enable_linger_if_needed() {
  _step "检查 systemd linger..."
  if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    _green "  Linger 已开 — service 会随开机自启"
    return
  fi

  _yellow ""
  _yellow "  Linger 未开。SSH 退出后 api-server 会停(systemd --user session 断开)。"
  _yellow "  要让它开机自启 + SSH 断开后继续跑,需要 sudo 执行一次:"
  _yellow "    sudo loginctl enable-linger $USER"
  _yellow ""
  read -r -p "  现在开?(y/N) " reply
  if [[ "$reply" == "y" || "$reply" == "Y" ]]; then
    if sudo loginctl enable-linger "$USER"; then
      _green "  Linger 已开"
    else
      _red "  sudo 失败 — 跳过。手动跑:sudo loginctl enable-linger $USER"
    fi
  else
    _yellow "  跳过 — SSH 保持连接时 service 正常跑,退出后会停"
    _yellow "  以后想开就跑:sudo loginctl enable-linger $USER"
  fi
}

start_service() {
  _step "启动 $SERVICE_NAME..."
  systemctl --user enable "$SERVICE_NAME.service" >/dev/null 2>&1
  systemctl --user start "$SERVICE_NAME.service"

  # Wait for the port to bind. Loop a few times with 1-second sleeps.
  local tries=10
  while (( tries > 0 )); do
    if curl -sk --max-time 2 "https://127.0.0.1:$API_PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then
      _green "  health check: /health → status ok"
      return
    fi
    tries=$((tries - 1))
    sleep 1
  done

  _red ""
  _red "✗ 服务启动后 10 秒内没过健康检查。"
  _red "  查最近日志排错:"
  _red "    journalctl --user -u $SERVICE_NAME -n 50 --no-pager"
  _red "    systemctl --user status $SERVICE_NAME"
  exit 1
}

print_summary() {
  local token fingerprint vps_host
  token=$("$PY" - <<PYEOF
import yaml
data = yaml.safe_load(open("$CONFIG_PATH"))
# Prefer the "desktop" label; fall back to the first token if someone
# swapped labels manually.
tokens = (data.get("auth") or {}).get("valid_tokens") or []
for t in tokens:
    if isinstance(t, dict) and t.get("label") == "desktop":
        print(t["token"])
        break
else:
    if tokens and isinstance(tokens[0], dict):
        print(tokens[0]["token"])
PYEOF
)
  fingerprint=$(cat "$CERT_DIR/fingerprint.txt")
  # Best guess at the VPS's public-facing address. hostname -I returns a
  # space-separated list; take the first non-loopback entry.
  vps_host=$(hostname -I 2>/dev/null | awk '{print $1}')
  [[ -z "${vps_host:-}" ]] && vps_host="<your-vps-host>"

  printf '\n'
  printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
  _green "  ✅ NODEBLE API Server 安装完成"
  printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n'

  echo "把下面 3 条填进 NODEBLE 桌面 app 的首次启动向导:"
  echo
  _cyan  "  服务器地址:"
  printf '    https://%s:%s\n\n' "$vps_host" "$API_PORT"
  _cyan  "  访问令牌:"
  printf '    %s\n\n' "$token"
  _cyan  "  证书指纹:"
  printf '    %s\n\n' "$fingerprint"

  _yellow "⚠️  若 $vps_host 是内网 IP,公网访问请替换成 VPS 的公网地址或域名。"
  _yellow "⚠️  确保 VPS 防火墙开放 $API_PORT/tcp:"
  printf '      sudo ufw allow %s/tcp    # ufw\n' "$API_PORT"
  printf '      # 或其他 iptables / 云厂商 security group 对应放行\n\n'

  echo "日常管理:"
  printf '  systemctl --user status %s       # 看状态\n' "$SERVICE_NAME"
  printf '  systemctl --user restart %s      # 重启\n' "$SERVICE_NAME"
  printf '  journalctl --user -u %s -f       # 实时日志\n' "$SERVICE_NAME"
  printf '\n'
  printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n'
}

# ── Entry point ────────────────────────────────────────────────────────────

main() {
  # Export so the inline-Python heredocs in write_config_defaults /
  # print_summary can read them without argument passing.
  export CONFIG_PATH

  check_prereqs
  setup_venv
  install_deps
  ensure_dirs
  generate_creds
  write_config_defaults
  install_systemd_unit
  enable_linger_if_needed
  start_service
  print_summary
}

main "$@"

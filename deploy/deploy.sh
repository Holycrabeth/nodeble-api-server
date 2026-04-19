#!/bin/bash
set -e

echo "========================================="
echo "  NODEBLE API Server — Deployment"
echo "========================================="
echo ""

# 1. Find Python 3.12+
find_python() {
    for cmd in python3.13 python3.12 python3; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" = "3" ] && [ "$minor" -ge 12 ] 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(find_python || true)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.12+ not found."
    echo "Install via:  sudo add-apt-repository ppa:deadsnakes/ppa"
    echo "              sudo apt install python3.12 python3.12-venv"
    exit 1
fi
echo "Python: $PYTHON ($($PYTHON --version))"

# 2. Locate project root (deploy.sh lives in <root>/deploy/)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_DIR="${API_DIR:-$(dirname "$SCRIPT_DIR")}"
if [ ! -f "$API_DIR/pyproject.toml" ]; then
    echo "ERROR: Could not find nodeble-api-server project root at $API_DIR"
    exit 1
fi
cd "$API_DIR"
echo "Project: $API_DIR"

# 3. Stop running service (if any)
sudo systemctl stop nodeble-api-server 2>/dev/null || true

# 4. Create venv + install
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install -e ".[dev]" -q

# 5. Create runtime dirs + copy config template if missing
API_DATA="$HOME/.nodeble-api"
mkdir -p "$API_DATA/config" "$API_DATA/certs"

API_CFG="$API_DATA/config/api.yaml"
if [ ! -f "$API_CFG" ]; then
    cp "$API_DIR/config/api.yaml.example" "$API_CFG"
    echo "Config template copied to $API_CFG"
else
    echo "Config already exists at $API_CFG (not overwritten)"
fi

# 6. Run tests
echo "Running tests..."
.venv/bin/pytest tests/ -q

# 7. Generate TLS cert if missing
CERT_PATH="$API_DATA/certs/cert.pem"
if [ ! -f "$CERT_PATH" ]; then
    echo ""
    echo "Generating self-signed TLS certificate..."
    .venv/bin/python -m nodeble_api_server generate-cert
else
    echo "TLS certificate already exists at $CERT_PATH"
fi

# 8. Generate a default token if no tokens are configured yet
TOKEN_COUNT=$(.venv/bin/python -c "
from nodeble_api_server.config import load_config
print(len(load_config().tokens))
")
NEW_TOKEN=""
if [ "$TOKEN_COUNT" = "0" ]; then
    echo ""
    echo "No API tokens configured — generating a default one..."
    # Capture the token from stdout (second line of output is the UUID)
    NEW_TOKEN=$(.venv/bin/python -m nodeble_api_server generate-token default | awk 'NR==2 {print $1}')
else
    echo "API tokens already configured ($TOKEN_COUNT total)"
fi

# 9. Install and start systemd service
echo ""
echo "Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/nodeble-api-server.service"
sed "s|NODEBLE_API_USER|$(whoami)|g; s|NODEBLE_API_VENV|$API_DIR/.venv|g; s|NODEBLE_API_DIR|$API_DIR|g" \
    "$API_DIR/deploy/nodeble-api-server.service" | sudo tee "$SERVICE_FILE" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable nodeble-api-server
sudo systemctl restart nodeble-api-server

# 10. Health check
echo "Waiting for service to come up..."
sleep 2
PORT=$(.venv/bin/python -c "from nodeble_api_server.config import load_config; print(load_config().server.port)")
HOST=$(.venv/bin/python -c "from nodeble_api_server.config import load_config; print(load_config().server.host)")
SCHEME="https"
if [ ! -f "$CERT_PATH" ]; then SCHEME="http"; fi

if curl -skf "${SCHEME}://127.0.0.1:$PORT/health" > /dev/null; then
    FINGERPRINT=""
    [ -f "$API_DATA/certs/fingerprint.txt" ] && FINGERPRINT=$(cat "$API_DATA/certs/fingerprint.txt")

    # Detect public-ish IP (first non-loopback)
    VPS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$VPS_IP" ] && VPS_IP="<your-vps-ip>"

    echo ""
    echo "========================================="
    echo "  DEPLOYMENT COMPLETE — SERVER IS LIVE"
    echo "========================================="
    echo "Listening on:  $HOST:$PORT ($SCHEME)"
    echo "Service:       nodeble-api-server"
    echo "Logs:          sudo journalctl -u nodeble-api-server -f"
    echo ""
    echo "--- Connect the desktop app ---"
    echo "VPS URL:       ${SCHEME}://$VPS_IP:$PORT"
    if [ -n "$NEW_TOKEN" ]; then
        echo "Token:         $NEW_TOKEN"
        echo "               (copy this into the app during first-run setup)"
    else
        echo "Token:         (already configured earlier — ask the admin)"
    fi
    if [ -n "$FINGERPRINT" ]; then
        echo "Fingerprint:   $FINGERPRINT"
        echo "               (verify this in the app to trust the TLS cert)"
    fi
    echo ""
    echo "Re-issue a token:   .venv/bin/python -m nodeble_api_server generate-token <label>"
    echo "Revoke a token:     .venv/bin/python -m nodeble_api_server revoke-token <label>"
else
    echo ""
    echo "========================================="
    echo "  DEPLOYMENT FAILED — /health not reachable"
    echo "========================================="
    echo "Debug:  sudo journalctl -u nodeble-api-server -n 50"
    exit 1
fi

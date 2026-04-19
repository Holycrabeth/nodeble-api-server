# nodeble-api-server

FastAPI sidecar for the NODEBLE Desktop App. Runs as a systemd service on the customer VPS and exposes REST + WebSocket access to all `nodeble-*` strategy modules (state files, configs, subprocess-triggered scans). One server per VPS, covers every strategy.

- Spec: `/home/mayongtao/projects/fullstack/APP_SPEC.md`
- YAML writes must go through `nodeble.bot.bot_helpers.set_config_value` — never reimplement validation.
- Install: `bash deploy/deploy.sh`
- Dev: `.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest tests/ -v`
- Run locally: `.venv/bin/python -m nodeble_api_server` (defaults to `0.0.0.0:8765`)

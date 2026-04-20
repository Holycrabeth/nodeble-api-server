"""Entry point — dispatch CLI subcommands or start the server.

Usage:
    python -m nodeble_api_server                         # start the server
    python -m nodeble_api_server generate-token <label>  # issue a token
    python -m nodeble_api_server revoke-token <label>    # revoke a token
    python -m nodeble_api_server generate-cert           # issue a TLS cert
"""
import argparse

import uvicorn

from nodeble_api_server import cli
from nodeble_api_server.config import load_config
from nodeble_api_server.logging_setup import install_token_redaction


def _run_server() -> None:
    # Install BEFORE uvicorn.run so uvicorn's own logger inherits the
    # sanitizing LogRecord factory set on the logging module root.
    install_token_redaction()

    cfg = load_config()
    kwargs: dict = {
        "host": cfg.server.host,
        "port": cfg.server.port,
        # access_log=False disables `uvicorn.access` (HTTP request log).
        # WebSocket handshake lines emit through `uvicorn.error` at INFO
        # and WOULD leak `?token=` — logging_setup.install_token_redaction
        # (above) catches those globally.
        "access_log": False,
        # WS keepalive: Mac lid-close + wake is a common scenario where TCP
        # won't notice the broken socket for minutes. Ping every 20s with a
        # 10s timeout forces a fast disconnect → client triggers reconnect.
        "ws_ping_interval": 20.0,
        "ws_ping_timeout": 10.0,
    }
    if cfg.tls.enabled:
        kwargs["ssl_certfile"] = str(cfg.tls.cert_path)
        kwargs["ssl_keyfile"] = str(cfg.tls.key_path)
    uvicorn.run("nodeble_api_server.app:app", **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nodeble_api_server",
        description="NODEBLE API Server — run `python -m nodeble_api_server` with no args to start the server.",
    )
    sub = parser.add_subparsers(dest="cmd")

    gt = sub.add_parser("generate-token", help="Generate a new API token")
    gt.add_argument("label", help="Human-readable label, e.g. 'my-macbook'")

    rt = sub.add_parser("revoke-token", help="Revoke a token by its label")
    rt.add_argument("label")

    sub.add_parser("generate-cert", help="Generate self-signed TLS certificate")

    args = parser.parse_args()

    if args.cmd == "generate-token":
        cli.generate_token(args.label)
    elif args.cmd == "revoke-token":
        cli.revoke_token(args.label)
    elif args.cmd == "generate-cert":
        cli.generate_cert()
    else:
        _run_server()


if __name__ == "__main__":
    main()

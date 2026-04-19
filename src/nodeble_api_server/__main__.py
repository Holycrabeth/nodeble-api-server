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


def _run_server() -> None:
    cfg = load_config()
    kwargs: dict = {
        "host": cfg.server.host,
        "port": cfg.server.port,
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

"""Command line entry point: ``agentboss``."""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .config import DEFAULT_CLAUDE_HOME, Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentboss",
        description="Dashboard for every Claude Code session on this machine.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8848, help="Port (default: %(default)s)")
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=DEFAULT_CLAUDE_HOME,
        help="Claude Code state directory (default: %(default)s)",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Minimum interval between filesystem rescans (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-control",
        action="store_true",
        help="Enable the endpoints that interrupt or terminate running sessions.",
    )
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser.")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev).")
    parser.add_argument("--log-level", default="warning", help="uvicorn log level")
    parser.add_argument("--version", action="version", version=f"agentboss {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    claude_home = args.claude_home.expanduser()
    if not claude_home.is_dir():
        print(f"agentboss: no Claude Code state directory at {claude_home}")
        return 1

    settings = Settings(
        claude_home=claude_home,
        refresh_interval=args.refresh,
        allow_control=args.allow_control,
    )

    url = f"http://{args.host}:{args.port}/"
    print(f"agentboss {__version__}  ->  {url}")
    print(f"  watching {claude_home}")
    if args.allow_control:
        print("  control endpoints ENABLED (interrupt / terminate)")
    if args.open:
        webbrowser.open(url)

    if args.reload:
        # Reload needs an import string rather than a live app object.
        import os

        os.environ["AGENTBOSS_CLAUDE_HOME"] = str(claude_home)
        os.environ["AGENTBOSS_REFRESH_INTERVAL"] = str(args.refresh)
        os.environ["AGENTBOSS_ALLOW_CONTROL"] = "1" if args.allow_control else "0"
        uvicorn.run(
            "agentboss.app:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level=args.log_level,
        )
    else:
        uvicorn.run(
            create_app(settings), host=args.host, port=args.port, log_level=args.log_level
        )
    return 0

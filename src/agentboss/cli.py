"""Command line entry point: `agentboss`."""

from __future__ import annotations

import argparse
import dataclasses
import os
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .config import DEFAULT_CLAUDE_HOME, DEFAULT_STATE_DIR, Settings


def build_parser() -> argparse.ArgumentParser:
    # The Settings flags default to None so main() can tell a flag the user
    # passed from one they left out; left-out flags fall back to the
    # AGENTBOSS_* environment variables, then to the built-in defaults.
    parser = argparse.ArgumentParser(
        prog="agentboss",
        description="Dashboard for every Claude Code session on this machine.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8848, help="Port (default: %(default)s)")
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=None,
        help=f"Claude Code state directory (env AGENTBOSS_CLAUDE_HOME, "
        f"default: {DEFAULT_CLAUDE_HOME})",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=f"Where agentboss keeps its own state (env AGENTBOSS_STATE_DIR, "
        f"default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Minimum interval between filesystem rescans "
        "(env AGENTBOSS_REFRESH_INTERVAL, default: 1.0)",
    )
    parser.add_argument(
        "--allow-control",
        action="store_true",
        default=None,
        help="Enable the endpoints that interrupt or terminate running sessions "
        "(env AGENTBOSS_ALLOW_CONTROL).",
    )
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser.")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev).")
    parser.add_argument("--log-level", default="warning", help="uvicorn log level")
    parser.add_argument("--version", action="version", version=f"agentboss {__version__}")
    return parser


def resolve_settings(args: argparse.Namespace) -> Settings:
    """Settings from the environment, overridden by any flags the user passed."""
    overrides: dict[str, object] = {}
    if args.claude_home is not None:
        overrides["claude_home"] = args.claude_home.expanduser()
    if args.state_dir is not None:
        overrides["state_dir"] = args.state_dir.expanduser()
    if args.refresh is not None:
        overrides["refresh_interval"] = args.refresh
    if args.allow_control is not None:
        overrides["allow_control"] = args.allow_control
    return dataclasses.replace(Settings.from_env(), **overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = resolve_settings(args)

    if not settings.claude_home.is_dir():
        print(f"agentboss: no Claude Code state directory at {settings.claude_home}")
        return 1

    url = f"http://{args.host}:{args.port}/"
    print(f"agentboss {__version__}  ->  {url}")
    print(f"  watching {settings.claude_home}")
    print(f"  state in {settings.state_dir}")
    if settings.allow_control:
        print("  control endpoints ENABLED (interrupt / terminate)")
    if args.open:
        webbrowser.open(url)

    if args.reload:
        # Reload needs an import string rather than a live app object, so the
        # resolved settings travel to the reloaded process via the environment.
        os.environ["AGENTBOSS_CLAUDE_HOME"] = str(settings.claude_home)
        os.environ["AGENTBOSS_STATE_DIR"] = str(settings.state_dir)
        os.environ["AGENTBOSS_REFRESH_INTERVAL"] = str(settings.refresh_interval)
        os.environ["AGENTBOSS_ALLOW_CONTROL"] = "1" if settings.allow_control else "0"
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

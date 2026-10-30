"""FastAPI application factory."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .api import router as api_router
from .config import Settings
from .store import SessionStore

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATES_DIR = PACKAGE_DIR / "templates"

DESCRIPTION = """
A single dashboard for every Claude Code session on this machine.

agentboss reads the Claude Code state directory (`~/.claude` by default) and
reports, live: which sessions exist, which are running, which models they are
burning tokens on, and how many agents and subagents each one has spawned.
It never writes to that directory.
"""


def asset_version(name: str) -> str:
    """A short content hash, appended to asset URLs so a changed file is never
    served from a stale browser cache."""
    return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]


def this_week(today: date | None = None) -> tuple[date, date]:
    """Monday of the current week through today: the dashboard's default range."""
    today = today or date.today()
    return today - timedelta(days=today.weekday()), today


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app.  Pass `settings` to point at a different Claude home."""
    settings = settings or Settings.from_env()
    store = SessionStore(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.scan(force=True)  # warm the cache before the first request
        yield

    app = FastAPI(
        title="agentboss",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.settings = settings

    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request) -> HTMLResponse:
        # The page script recomputes this in the browser's own timezone; the
        # server's copy means the inputs are never blank while it loads.
        range_from, range_to = this_week()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "claude_home": str(settings.claude_home),
                "allow_control": settings.allow_control,
                "range_from": range_from.isoformat(),
                "range_to": range_to.isoformat(),
                "css_version": asset_version("app.css"),
                "js_version": asset_version("app.js"),
            },
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True, "version": __version__, "claude_home": str(settings.claude_home)}

    return app


app = create_app()

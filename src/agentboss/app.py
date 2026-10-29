"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app.  Pass ``settings`` to point at a different Claude home."""
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
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "claude_home": str(settings.claude_home),
                "allow_control": settings.allow_control,
            },
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True, "version": __version__, "claude_home": str(settings.claude_home)}

    return app


app = create_app()

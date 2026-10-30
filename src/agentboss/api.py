"""JSON API.

Read endpoints are unconditional.  The two control endpoints (interrupt and
terminate a running session) are only mounted when the server was started with
`--allow-control`, because they signal real processes that may be mid-edit.
"""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .models import AgentSummary, Overview, QuotaReport, Recommendation, SessionSummary
from .registry import signal_session
from .store import SessionStore

router = APIRouter(prefix="/api", tags=["agentboss"])

_SIGNALS = {"interrupt": signal.SIGINT, "terminate": signal.SIGTERM}

#: Bookkeeping fields that change on every scan whether or not anything happened.
#: They are sent, but excluded from the "has anything changed?" comparison:
#: otherwise every tick would look like news and the client would repaint forever.
_VOLATILE_FIELDS = frozenset({"generated_at", "scan_seconds"})


def get_store(request: Request) -> SessionStore:
    return request.app.state.store


RangeStart = Annotated[
    datetime | None, Query(description="Range start (ISO 8601). Naive times are UTC.")
]
RangeEnd = Annotated[
    datetime | None, Query(description="Range end, exclusive (ISO 8601). Defaults to now.")
]


def _range(
    start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Make a requested range timezone-aware and reject an inverted one."""
    start = start.replace(tzinfo=UTC) if start and start.tzinfo is None else start
    end = end.replace(tzinfo=UTC) if end and end.tzinfo is None else end
    if start and end and end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    return start, end


@router.get("/overview", response_model=Overview)
def overview(
    request: Request,
    hours: int = Query(24, ge=1, le=168, description="Width of the activity window."),
    start: RangeStart = None,
    end: RangeEnd = None,
) -> Overview:
    """Headline counts, model mix, agent-type mix and the activity timeline.

    A `start`/`end` range replaces the trailing `hours` window and scopes the
    model mix and token totals to that range."""
    start, end = _range(start, end)
    return get_store(request).overview(hours=hours, start=start, end=end)


@router.get("/quota", response_model=QuotaReport)
def quota(
    request: Request,
    days: int = Query(30, ge=1, le=90, description="Days of daily spend history."),
    start: RangeStart = None,
    end: RangeEnd = None,
) -> QuotaReport:
    """Quota consumption: spend in the current five-hour block and seven-day
    window, estimated caps from recorded limit hits, and daily history.

    A `start`/`end` range sets the daily history instead of `days`; the
    limit-window meters always describe the live windows."""
    start, end = _range(start, end)
    return get_store(request).quota(days=days, start=start, end=end)


@router.get("/recommendations", response_model=list[Recommendation])
def recommendations(request: Request) -> list[Recommendation]:
    """Concrete ways to spend less quota, most urgent first."""
    return get_store(request).recommendations()


class DoneUpdate(BaseModel):
    done: bool = True


@router.put("/recommendations/{rec_id:path}/done", status_code=204)
def set_recommendation_done(request: Request, rec_id: str, body: DoneUpdate) -> None:
    """Tick a recommendation off (`done: true`) or put it back (`done: false`).

    Ticks are kept in agentboss's own state directory and last a week."""
    get_store(request).set_done(rec_id, body.done)


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(
    request: Request,
    status: str | None = Query(None, description="busy | idle | stale | ended | live | all"),
    project: str | None = None,
    model: str | None = None,
    agent_type: str | None = None,
    q: str | None = Query(None, description="Substring match on id, project, title or prompt."),
    start: RangeStart = None,
    end: RangeEnd = None,
    sort: str = "last_activity",
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[SessionSummary]:
    """Every session agentboss can see, filtered and sorted."""
    start, end = _range(start, end)
    return get_store(request).query_sessions(
        status=status,
        project=project,
        model=model,
        agent_type=agent_type,
        search=q,
        start=start,
        end=end,
        sort=sort,
        descending=order == "desc",
        limit=limit,
        offset=offset,
    )


@router.get("/sessions/{session_id}", response_model=SessionSummary)
def get_session(request: Request, session_id: str) -> SessionSummary:
    """One session, including every subagent it spawned."""
    session = get_store(request).get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
    return session


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(
    request: Request,
    session_id: str | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    live_only: bool = False,
    limit: int | None = Query(None, ge=1, le=1000),
) -> list[AgentSummary]:
    """Subagents across every session, most recently active first."""
    return get_store(request).query_agents(
        session_id=session_id,
        agent_type=agent_type,
        model=model,
        live_only=live_only,
        limit=limit,
    )


@router.get("/models")
def list_models(request: Request) -> dict:
    """Token and message totals per model, across sessions and subagents."""
    data = get_store(request).overview(hours=1)
    return {"models": [usage.model_dump() for usage in data.models]}


@router.get("/projects")
def list_projects(request: Request) -> dict:
    """Sessions grouped by working directory."""
    data = get_store(request).overview(hours=1)
    return {"projects": [project.model_dump() for project in data.projects]}


@router.get("/filters")
def list_filters(request: Request) -> dict:
    """The distinct values the UI offers in its filter controls."""
    store = get_store(request)
    return {
        "projects": store.known_projects(),
        "models": store.known_models(),
        "agent_types": store.known_agent_types(),
        "statuses": ["all", "live", "busy", "idle", "stale", "ended"],
    }


@router.get("/stream")
async def stream(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    start: RangeStart = None,
    end: RangeEnd = None,
) -> StreamingResponse:
    """Server-sent events: a fresh overview whenever the scan finds a change."""
    start, end = _range(start, end)
    store = get_store(request)
    interval = max(1.0, store.settings.refresh_interval)

    async def events():
        last_fingerprint: str | None = None
        while True:
            if await request.is_disconnected():
                return
            data = await asyncio.to_thread(store.overview, hours=hours, start=start, end=end)
            dump = data.model_dump(mode="json")
            fingerprint = json.dumps(
                {k: v for k, v in dump.items() if k not in _VOLATILE_FIELDS}, default=str
            )
            if fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                yield f"event: overview\ndata: {json.dumps(dump, default=str)}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(interval)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/{action}")
def control_session(request: Request, session_id: str, action: str) -> dict:
    """Interrupt or terminate a running session.  Requires --allow-control."""
    store = get_store(request)
    if not store.settings.allow_control:
        raise HTTPException(
            status_code=403,
            detail="Control actions are disabled. Restart agentboss with --allow-control.",
        )
    if action not in _SIGNALS:
        raise HTTPException(
            status_code=400, detail=f"Unknown action {action!r}; expected one of {sorted(_SIGNALS)}"
        )

    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
    if not session.is_live or session.pid is None:
        raise HTTPException(status_code=409, detail="Session is not running")

    try:
        signal_session(session.pid, _SIGNALS[action])
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not signal pid {session.pid}: {exc}"
        ) from exc

    store.scan(force=True)
    return {"session_id": session_id, "pid": session.pid, "action": action, "sent": True}

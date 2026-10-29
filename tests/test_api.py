"""HTTP surface: the JSON API, the dashboard page and the control guard."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from agentboss.app import create_app
from tests.conftest import ENDED_ID, LIVE_ID


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def test_dashboard_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "agentboss" in response.text
    assert "/static/app.js" in response.text


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True


def test_overview(client):
    body = client.get("/api/overview").json()
    assert body["total_sessions"] == 3
    assert body["live_sessions"] == 1
    assert body["total_agents"] == 1
    assert body["agent_types"] == {"Explore": 1}
    assert body["tokens"]["total"] > 0
    assert len(body["activity"]) == 24


def test_overview_window_is_configurable(client):
    assert len(client.get("/api/overview?hours=6").json()["activity"]) == 6
    assert client.get("/api/overview?hours=0").status_code == 422


def test_list_sessions_and_filters(client):
    everything = client.get("/api/sessions?status=all").json()
    assert len(everything) == 3

    live = client.get("/api/sessions?status=live").json()
    assert [s["session_id"] for s in live] == [LIVE_ID]
    assert live[0]["is_live"] is True
    assert live[0]["resume_command"] == f"claude --resume {LIVE_ID}"

    assert client.get("/api/sessions?q=demo+session").json()[0]["session_id"] == LIVE_ID
    assert client.get("/api/sessions?project=/work/other").json()[0]["session_id"] == ENDED_ID
    assert client.get("/api/sessions?agent_type=Explore").json()[0]["session_id"] == LIVE_ID
    assert client.get("/api/sessions?status=all&limit=2").json().__len__() == 2


def test_session_detail_includes_agents(client):
    body = client.get(f"/api/sessions/{LIVE_ID}").json()
    assert body["agent_count"] == 1
    assert body["agents"][0]["agent_type"] == "Explore"
    assert body["agents"][0]["duration_seconds"] == 60.0
    assert body["combined_tokens"] > body["tokens"]["total"]


def test_unknown_session_is_404(client):
    assert client.get("/api/sessions/does-not-exist").status_code == 404


def test_agents_endpoint(client):
    agents = client.get("/api/agents").json()
    assert len(agents) == 1
    assert agents[0]["session_id"] == LIVE_ID
    assert client.get("/api/agents?agent_type=Plan").json() == []


def test_models_and_projects(client):
    models = client.get("/api/models").json()["models"]
    assert {m["model"] for m in models} == {
        "claude-opus-5",
        "claude-fable-5",
        "claude-opus-4-8",
    }
    projects = client.get("/api/projects").json()["projects"]
    assert {p["project"] for p in projects} == {"/work/demo", "/work/other", "/work/ghost"}


def test_filters_endpoint(client):
    body = client.get("/api/filters").json()
    assert body["agent_types"] == ["Explore"]
    assert "live" in body["statuses"]
    assert "/work/demo" in body["projects"]


def test_control_is_refused_unless_enabled(client):
    response = client.post(f"/api/sessions/{LIVE_ID}/interrupt")
    assert response.status_code == 403
    assert "--allow-control" in response.json()["detail"]


def test_control_rejects_unknown_actions_and_dead_sessions(settings):
    enabled = dataclasses.replace(settings, allow_control=True)
    with TestClient(create_app(enabled)) as client:
        assert client.post(f"/api/sessions/{LIVE_ID}/explode").status_code == 400
        assert client.post(f"/api/sessions/{ENDED_ID}/interrupt").status_code == 409
        assert client.post("/api/sessions/nope/interrupt").status_code == 404


def test_control_signals_a_live_session(settings, monkeypatch):
    """With control enabled, a live session gets exactly the signal asked for."""
    import agentboss.api as api

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(api, "signal_session", lambda pid, sig: sent.append((pid, sig)))

    enabled = dataclasses.replace(settings, allow_control=True)
    with TestClient(create_app(enabled)) as client:
        body = client.post(f"/api/sessions/{LIVE_ID}/terminate").json()

    import signal as signal_module

    assert body["sent"] is True
    assert sent == [(body["pid"], signal_module.SIGTERM)]


async def test_stream_pushes_an_overview(settings):
    """The SSE endpoint frames and sends an overview before its first sleep.

    Driven through the response's body iterator rather than an HTTP client: the
    stream is endless by design, and httpx's ASGI transport buffers a response
    body to completion, so a client-side read of it never returns.
    """
    import json

    import anyio
    from starlette.requests import Request

    from agentboss.api import stream

    app = create_app(settings)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": b"hours=1",
        "root_path": "",
        "scheme": "http",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "app": app,
    }

    async def receive():  # pragma: no cover - starlette cancels before awaiting
        return {"type": "http.request"}

    response = await stream(Request(scope, receive), hours=1)
    assert response.media_type == "text/event-stream"

    body = response.body_iterator
    try:
        with anyio.fail_after(15):
            chunk = await body.__anext__()
    finally:
        await body.aclose()

    assert chunk.startswith("event: overview\ndata: ")
    payload = json.loads(chunk.split("data: ", 1)[1])
    assert payload["total_sessions"] == 3
    assert payload["live_sessions"] == 1
    assert len(payload["activity"]) == 1


async def test_stream_holds_quiet_when_nothing_changed(settings):
    """A tick with no real change sends a keep-alive, not another repaint.

    ``generated_at`` and ``scan_seconds`` differ on every scan, so comparing whole
    payloads would make every tick look like news and the client would rebuild its
    charts once a second forever.
    """
    import anyio
    from starlette.requests import Request

    from agentboss.api import stream

    app = create_app(settings)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": b"hours=1",
        "root_path": "",
        "scheme": "http",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "app": app,
    }

    async def receive():  # pragma: no cover - starlette cancels before awaiting
        return {"type": "http.request"}

    response = await stream(Request(scope, receive), hours=1)
    body = response.body_iterator
    try:
        with anyio.fail_after(20):
            first = await body.__anext__()
            second = await body.__anext__()
    finally:
        await body.aclose()

    assert first.startswith("event: overview")
    assert second == ": keep-alive\n\n"


def test_quota_endpoint(client):
    body = client.get("/api/quota?days=7").json()
    assert {"five_hour", "seven_day", "limit_hits", "daily", "blocks"} <= body.keys()
    assert len(body["daily"]) == 7
    assert body["seven_day"]["kind"] == "seven_day"

"""A synthetic `~/.claude` tree, so tests never touch the real one."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentboss.config import Settings
from agentboss.store import SessionStore

BASE = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def iso(offset_seconds: float) -> str:
    return (BASE + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def assistant_record(
    *,
    session_id: str,
    message_id: str,
    model: str = "claude-opus-5",
    blocks: list[dict] | None = None,
    usage: dict | None = None,
    offset: float = 0.0,
    cwd: str = "/work/demo",
    sidechain: bool = False,
    effort: str | None = "high",
) -> dict:
    """One assistant record.

    The real format writes one record *per content block*, each repeating the
    same `message.usage`; helpers below rely on that to test de-duplication.
    """
    return {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": cwd,
        "gitBranch": "main",
        "version": "2.1.252",
        "isSidechain": sidechain,
        "effort": effort,
        "timestamp": iso(offset),
        "uuid": f"{message_id}-{offset}",
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": model,
            "content": blocks if blocks is not None else [{"type": "text", "text": "hi"}],
            "usage": usage
            or {
                "input_tokens": 10,
                "output_tokens": 100,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 500,
                "output_tokens_details": {"thinking_tokens": 40},
            },
        },
    }


def user_record(session_id: str, offset: float = 0.0, cwd: str = "/work/demo") -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": iso(offset),
        "uuid": f"u-{offset}",
        "message": {"role": "user", "content": "do the thing"},
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


@pytest.fixture(autouse=True)
def fake_processes(monkeypatch):
    """Report this test process as a running Claude CLI.

    The fixture tree registers a session against our own pid; without this the
    liveness check would correctly reject it (pytest is not `claude`), and the
    tests could not exercise the live path at all.
    """
    import agentboss.registry as registry

    monkeypatch.setattr(registry, "running_commands", lambda: {os.getpid(): "claude"})


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """A tree with two sessions: one live with a subagent, one ended."""
    home = tmp_path / "claude"
    project = home / "projects" / "-work-demo"
    other = home / "projects" / "-work-other"

    # --- live session, two API turns (each split across two records) + 1 subagent
    live_id = "11111111-1111-1111-1111-111111111111"
    write_jsonl(
        project / f"{live_id}.jsonl",
        [
            user_record(live_id, 0),
            assistant_record(
                session_id=live_id,
                message_id="msg-a",
                blocks=[{"type": "thinking", "thinking": "..."}],
                offset=1,
            ),
            assistant_record(
                session_id=live_id,
                message_id="msg-a",
                blocks=[{"type": "tool_use", "name": "Bash", "input": {}}],
                offset=2,
            ),
            assistant_record(
                session_id=live_id,
                message_id="msg-b",
                model="claude-fable-5",
                blocks=[
                    {"type": "tool_use", "name": "Task", "input": {"subagent_type": "Explore"}}
                ],
                offset=3,
            ),
            {"type": "ai-title", "sessionId": live_id, "aiTitle": "Demo session"},
            {"type": "last-prompt", "sessionId": live_id, "lastPrompt": "do the thing"},
            {"type": "permission-mode", "sessionId": live_id, "permissionMode": "acceptEdits"},
        ],
    )

    subagents = project / live_id / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        subagents / "agent-abc123.jsonl",
        [
            assistant_record(
                session_id=live_id,
                message_id="msg-s1",
                model="claude-fable-5",
                blocks=[{"type": "tool_use", "name": "Grep", "input": {}}],
                offset=10,
                sidechain=True,
            ),
            assistant_record(
                session_id=live_id,
                message_id="msg-s2",
                model="claude-fable-5",
                blocks=[{"type": "text", "text": "found it"}],
                offset=70,
                sidechain=True,
            ),
        ],
    )
    (subagents / "agent-abc123.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "Find the config",
                "toolUseId": "toolu_1",
                "spawnDepth": 1,
            }
        )
    )

    # --- ended session in another project, no subagents
    ended_id = "22222222-2222-2222-2222-222222222222"
    write_jsonl(
        other / f"{ended_id}.jsonl",
        [
            user_record(ended_id, 0, cwd="/work/other"),
            assistant_record(
                session_id=ended_id,
                message_id="msg-c",
                model="claude-opus-4-8",
                offset=5,
                cwd="/work/other",
            ),
        ],
    )

    # --- live registry: our own pid, so the liveness check genuinely passes
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "sessionId": live_id,
                "cwd": "/work/demo",
                "startedAt": int(BASE.timestamp() * 1000),
                "version": "2.1.252",
                "kind": "interactive",
                "entrypoint": "cli",
                "name": "demo-live",
                "status": "busy",
                "updatedAt": int(BASE.timestamp() * 1000),
            }
        )
    )
    # a registry entry whose process is long gone
    (sessions / "999999.json").write_text(
        json.dumps(
            {
                "pid": 999999,
                "sessionId": "33333333-3333-3333-3333-333333333333",
                "cwd": "/work/ghost",
                "status": "idle",
                "name": "ghost",
            }
        )
    )

    tasks = home / "tasks" / live_id
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "1.json").write_text(json.dumps({"id": "1", "status": "in_progress"}))
    (tasks / "2.json").write_text(json.dumps({"id": "2", "status": "completed"}))

    return home


@pytest.fixture
def settings(claude_home: Path) -> Settings:
    return Settings(claude_home=claude_home, refresh_interval=0.0)


@pytest.fixture
def store(settings: Settings) -> SessionStore:
    store = SessionStore(settings)
    store.scan(force=True)
    return store


LIVE_ID = "11111111-1111-1111-1111-111111111111"
ENDED_ID = "22222222-2222-2222-2222-222222222222"
GHOST_ID = "33333333-3333-3333-3333-333333333333"

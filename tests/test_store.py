"""Scanning, aggregation and querying against a synthetic Claude home."""

from __future__ import annotations

import json

from agentboss.models import SessionStatus
from agentboss.store import SessionStore
from tests.conftest import ENDED_ID, GHOST_ID, LIVE_ID, assistant_record, write_jsonl


def test_finds_every_session(store: SessionStore):
    ids = {s.session_id for s in store.all_sessions()}
    assert ids == {LIVE_ID, ENDED_ID, GHOST_ID}


def test_status_reflects_the_process_not_the_file(store: SessionStore):
    by_id = {s.session_id: s for s in store.all_sessions()}
    # The live registry entry points at this very process, so it must read live.
    assert by_id[LIVE_ID].status is SessionStatus.BUSY
    assert by_id[LIVE_ID].is_live
    assert by_id[LIVE_ID].name == "demo-live"
    # A registry entry for a pid that does not exist is stale, not idle...
    assert by_id[GHOST_ID].status is SessionStatus.STALE
    # ...and a transcript with no registry entry at all has simply ended.
    assert by_id[ENDED_ID].status is SessionStatus.ENDED


def test_session_metadata_and_counters(store: SessionStore):
    session = store.get_session(LIVE_ID)
    assert session is not None
    assert session.project == "/work/demo"
    assert session.title == "Demo session"
    assert session.last_prompt == "do the thing"
    assert session.permission_mode == "acceptEdits"
    assert session.git_branch == "main"
    assert session.user_messages == 1
    assert session.assistant_messages == 2, "two turns; the first spans two records"
    assert session.tool_calls == 2
    assert session.spawn_count == 1, "one Task tool call"
    assert session.open_tasks == 1, "completed tasks are not open"
    assert session.transcript_bytes > 0


def test_subagents_are_attached_with_their_metadata(store: SessionStore):
    session = store.get_session(LIVE_ID)
    assert session.agent_count == 1
    agent = session.agents[0]
    assert agent.agent_id == "agent-abc123"
    assert agent.agent_type == "Explore"
    assert agent.description == "Find the config"
    assert agent.spawn_depth == 1
    assert agent.messages == 2
    assert agent.tool_calls == 1
    assert agent.duration_seconds == 60.0
    assert [m.model for m in agent.models] == ["claude-fable-5"]
    assert agent.tokens.total > 0


def test_agent_tokens_are_separate_from_the_main_loop(store: SessionStore):
    session = store.get_session(LIVE_ID)
    assert session.tokens.total > 0
    assert session.agent_tokens.total > 0
    assert session.combined_tokens == session.tokens.total + session.agent_tokens.total


def test_models_are_per_session_and_sorted_by_tokens(store: SessionStore):
    session = store.get_session(LIVE_ID)
    assert {m.model for m in session.models} == {"claude-opus-5", "claude-fable-5"}
    totals = [m.tokens.total for m in session.models]
    assert totals == sorted(totals, reverse=True)


def test_overview_totals(store: SessionStore):
    overview = store.overview()
    assert overview.total_sessions == 3
    assert overview.live_sessions == 1
    assert overview.busy_sessions == 1
    assert overview.total_agents == 1
    assert overview.agent_types == {"Explore": 1}
    # Three models across the tree: two in the live session, one in the ended one.
    assert {m.model for m in overview.models} == {
        "claude-opus-5",
        "claude-fable-5",
        "claude-opus-4-8",
    }
    assert overview.tokens.total > 0
    assert {p.project for p in overview.projects} == {"/work/demo", "/work/other", "/work/ghost"}


def test_filters(store: SessionStore):
    assert [s.session_id for s in store.query_sessions(status="live")] == [LIVE_ID]
    assert [s.session_id for s in store.query_sessions(status="ended")] == [ENDED_ID]
    assert [s.session_id for s in store.query_sessions(project="/work/other")] == [ENDED_ID]
    assert [s.session_id for s in store.query_sessions(model="claude-opus-4-8")] == [ENDED_ID]
    assert [s.session_id for s in store.query_sessions(agent_type="Explore")] == [LIVE_ID]
    assert [s.session_id for s in store.query_sessions(search="demo session")] == [LIVE_ID]
    assert store.query_sessions(search="nothing-matches-this") == []


def test_model_filter_matches_subagent_models(store: SessionStore):
    """A model only a subagent used still selects its parent session."""
    found = store.query_sessions(model="claude-fable-5")
    assert [s.session_id for s in found] == [LIVE_ID]


def test_sorting_and_paging(store: SessionStore):
    by_tokens = store.query_sessions(status="all", sort="tokens")
    totals = [s.combined_tokens for s in by_tokens]
    assert totals == sorted(totals, reverse=True)

    ascending = store.query_sessions(status="all", sort="tokens", descending=False)
    assert [s.session_id for s in ascending] == [s.session_id for s in reversed(by_tokens)]

    assert len(store.query_sessions(status="all", limit=1)) == 1
    assert len(store.query_sessions(status="all", offset=1)) == 2


def test_query_agents(store: SessionStore):
    assert len(store.query_agents()) == 1
    assert len(store.query_agents(session_id=LIVE_ID)) == 1
    assert len(store.query_agents(session_id=ENDED_ID)) == 0
    assert len(store.query_agents(agent_type="Explore")) == 1
    assert len(store.query_agents(agent_type="Plan")) == 0
    assert len(store.query_agents(model="claude-fable-5")) == 1
    assert len(store.query_agents(live_only=True)) == 1


def test_filter_vocabularies(store: SessionStore):
    assert store.known_agent_types() == ["Explore"]
    assert "/work/demo" in store.known_projects()
    assert "claude-opus-5" in store.known_models()


def test_rescan_picks_up_appended_turns(store: SessionStore, claude_home):
    before = store.get_session(LIVE_ID).assistant_messages
    write_jsonl(
        claude_home / "projects" / "-work-demo" / f"{LIVE_ID}.jsonl",
        [assistant_record(session_id=LIVE_ID, message_id="msg-new", offset=400)],
    )
    store.scan(force=True)
    assert store.get_session(LIVE_ID).assistant_messages == before + 1


def test_rescan_drops_a_deleted_session(store: SessionStore, claude_home):
    (claude_home / "projects" / "-work-other" / f"{ENDED_ID}.jsonl").unlink()
    store.scan(force=True)
    assert store.get_session(ENDED_ID) is None


def test_new_subagent_appears_on_rescan(store: SessionStore, claude_home):
    subagents = claude_home / "projects" / "-work-demo" / LIVE_ID / "subagents"
    write_jsonl(
        subagents / "agent-def456.jsonl",
        [assistant_record(session_id=LIVE_ID, message_id="msg-n1", offset=500, sidechain=True)],
    )
    (subagents / "agent-def456.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "nested", "spawnDepth": 2})
    )
    store.scan(force=True)

    session = store.get_session(LIVE_ID)
    assert session.agent_count == 2
    assert session.subagent_count == 1, "one agent is nested below another"
    assert session.max_spawn_depth == 2
    assert store.overview().agent_types == {"Explore": 1, "general-purpose": 1}


def test_missing_claude_home_is_not_an_error(tmp_path):
    from agentboss.config import Settings

    store = SessionStore(Settings(claude_home=tmp_path / "nope", refresh_interval=0.0))
    store.scan(force=True)
    assert store.all_sessions() == []
    assert store.overview().total_sessions == 0

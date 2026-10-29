"""Recommendation rules, on hand-built stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentboss.models import AgentSummary, SessionStatus, SessionSummary
from agentboss.parser import IncrementalTranscript, TranscriptStats
from agentboss.quota import Spend
from agentboss.recommend import SessionData, build_recommendations

from .conftest import assistant_record, write_jsonl

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
WEEK = (NOW - timedelta(days=3), NOW + timedelta(days=4))


def session(sid: str = "s1", *, live: bool = True, title: str = "work") -> SessionSummary:
    return SessionSummary(
        session_id=sid,
        project="/p",
        project_slug="p",
        title=title,
        status=SessionStatus.BUSY if live else SessionStatus.ENDED,
        last_activity_at=NOW - timedelta(minutes=5),
    )


def stats(
    *, context: int = 0, cost: float = 0.0, model: str = "claude-opus-5-5"
) -> TranscriptStats:
    st = TranscriptStats(context_tokens=context)
    if cost:
        st.spend[(NOW - timedelta(hours=1), model)] = Spend(turns=1, tokens=1, cost=cost)
    return st


def agent(name: str, **kw) -> AgentSummary:
    return AgentSummary(
        agent_id=f"agent-{name}", session_id="s1", project="/p", agent_type=name, **kw
    )


def kinds(recs):
    return [r.kind for r in recs]


def test_deep_context_recommends_compacting_with_per_turn_saving():
    data = [SessionData(summary=session(), main=stats(context=500_000, cost=10))]
    recs = build_recommendations(data, week=WEEK, now=NOW)
    ctx = next(r for r in recs if r.kind == "context")
    assert ctx.severity == "high"
    # (500k - 40k) cache-read tokens at Opus 5.5 ($0.40/M) over 100 turns.
    assert ctx.estimated_savings == round(460_000 * 0.4 / 1e6 * 100, 2)


def test_shallow_or_old_sessions_are_left_alone():
    old = session(live=False)
    old.last_activity_at = NOW - timedelta(days=3)
    data = [
        SessionData(summary=session("a"), main=stats(context=60_000)),
        SessionData(summary=old, main=stats(context=900_000)),
    ]
    assert "context" not in kinds(build_recommendations(data, week=WEEK, now=NOW))


def test_session_holding_most_of_the_window_is_called_out():
    data = [
        SessionData(summary=session("big", title="big"), main=stats(cost=300)),
        SessionData(summary=session("small", title="small"), main=stats(cost=30)),
    ]
    recs = [r for r in build_recommendations(data, week=WEEK, now=NOW) if r.kind == "window"]
    assert [r.session_id for r in recs] == ["big"]
    assert "91%" in recs[0].title


def test_premium_subagents_and_teammates_are_grouped_by_definition():
    agents = [
        (agent("general-purpose"), stats(cost=100)),
        (
            agent(
                "join-where",
                base_type="general-purpose",
                task_kind="in_process_teammate",
                requested_model="opus",
            ),
            stats(cost=40),
        ),
        (
            agent(
                "p4-order",
                base_type="general-purpose",
                task_kind="in_process_teammate",
                requested_model="opus",
            ),
            stats(cost=40),
        ),
        (agent("Explore"), stats(cost=100, model="claude-sonnet-5-5")),  # already cheap
    ]
    data = [SessionData(summary=session(), main=stats(), agents=agents)]
    recs = [
        r for r in build_recommendations(data, week=WEEK, now=NOW) if r.kind == "subagent_model"
    ]
    titles = {r.title: r for r in recs}
    assert set(titles) == {
        "Run general-purpose agents on Sonnet",
        "Run general-purpose teammates on Sonnet",
    }
    # Opus 5.5 input $4 -> Sonnet 5.5 $2: half the cost.
    assert titles["Run general-purpose agents on Sonnet"].estimated_savings == 50
    assert "join-where, p4-order" in titles["Run general-purpose teammates on Sonnet"].detail


def test_large_tool_output_is_flagged():
    main = stats()
    main.tool_result_chars["Bash"] = 10 * 40_000
    main.tool_result_count["Bash"] = 10
    main.tool_result_chars["Edit"] = 10 * 400
    main.tool_result_count["Edit"] = 10
    data = [SessionData(summary=session(), main=main)]
    recs = [r for r in build_recommendations(data, week=WEEK, now=NOW) if r.kind == "tool_output"]
    assert [r.title for r in recs] == ["Bash results are large"]


def test_parser_tracks_context_and_tool_result_sizes(tmp_path):
    path = tmp_path / "s.jsonl"
    call = assistant_record(
        session_id="s",
        message_id="m1",
        blocks=[{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
        usage={
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 100,
        },
    )
    result = {
        "type": "user",
        "sessionId": "s",
        "timestamp": call["timestamp"],
        "toolUseResult": {},
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": "x" * 1234}],
                },
            ],
        },
    }
    write_jsonl(path, [call, result])
    reader = IncrementalTranscript(path)
    reader.refresh()
    assert reader.stats.context_tokens == 2105
    assert reader.stats.tool_result_chars["Bash"] == 1234
    assert reader.stats.tool_result_count["Bash"] == 1

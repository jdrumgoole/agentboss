"""The parser's two load-bearing behaviours: usage de-duplication and incrementality."""

from __future__ import annotations

import json

from agentboss.parser import IncrementalTranscript, parse_timestamp
from tests.conftest import assistant_record, user_record, write_jsonl


def test_usage_is_credited_once_per_api_turn(tmp_path):
    """One turn split across three records must not count its usage three times."""
    path = tmp_path / "s.jsonl"
    blocks = [
        [{"type": "thinking", "thinking": "..."}],
        [{"type": "text", "text": "hello"}],
        [{"type": "tool_use", "name": "Bash", "input": {}}],
    ]
    write_jsonl(
        path,
        [
            assistant_record(session_id="s", message_id="msg-1", blocks=block, offset=i)
            for i, block in enumerate(blocks)
        ],
    )

    reader = IncrementalTranscript(path)
    reader.refresh()
    stats = reader.stats

    assert stats.assistant_messages == 1, "three records, one API turn"
    assert stats.output_tokens == 100
    assert stats.input_tokens == 10
    assert stats.cache_read_tokens == 1000
    assert stats.thinking_tokens == 40
    assert stats.models["claude-opus-5"].messages == 1
    # Tool calls really are per-record, so they are not de-duplicated.
    assert stats.tool_calls == 1
    assert stats.tools["Bash"] == 1


def test_distinct_turns_accumulate(tmp_path):
    path = tmp_path / "s.jsonl"
    write_jsonl(
        path,
        [
            assistant_record(session_id="s", message_id="msg-1", offset=0),
            assistant_record(session_id="s", message_id="msg-2", offset=1),
        ],
    )
    reader = IncrementalTranscript(path)
    reader.refresh()
    assert reader.stats.assistant_messages == 2
    assert reader.stats.output_tokens == 200


def test_refresh_only_parses_appended_bytes(tmp_path):
    path = tmp_path / "s.jsonl"
    write_jsonl(path, [assistant_record(session_id="s", message_id="msg-1", offset=0)])

    reader = IncrementalTranscript(path)
    assert reader.refresh() is True
    assert reader.lines_parsed == 1
    assert reader.refresh() is False, "nothing appended, nothing to do"
    assert reader.lines_parsed == 1

    write_jsonl(path, [assistant_record(session_id="s", message_id="msg-2", offset=1)])
    assert reader.refresh() is True
    assert reader.lines_parsed == 2, "only the new line was parsed"
    assert reader.stats.assistant_messages == 2


def test_partial_trailing_line_is_not_consumed(tmp_path):
    """A transcript being written mid-record must not corrupt the aggregate."""
    path = tmp_path / "s.jsonl"
    record = json.dumps(assistant_record(session_id="s", message_id="msg-1", offset=0))
    path.write_text(record + "\n" + record[: len(record) // 2])

    reader = IncrementalTranscript(path)
    reader.refresh()
    assert reader.stats.assistant_messages == 1

    # Completing the line makes the second turn visible without re-reading the first.
    with path.open("a") as handle:
        handle.write(record[len(record) // 2 :] + "\n")
    reader.refresh()
    assert reader.stats.assistant_messages == 1, "same message id, same turn"
    assert reader.stats.output_tokens == 100


def test_truncated_file_is_reread_from_scratch(tmp_path):
    path = tmp_path / "s.jsonl"
    write_jsonl(path, [assistant_record(session_id="s", message_id="msg-1", offset=0)])
    reader = IncrementalTranscript(path)
    reader.refresh()

    path.write_text("")
    write_jsonl(path, [assistant_record(session_id="s", message_id="msg-9", offset=0)])
    reader.refresh()
    assert reader.stats.assistant_messages == 1
    assert "msg-9" not in reader.stats.models  # sanity: models are keyed by model name
    assert reader.stats.output_tokens == 100


def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    good = json.dumps(assistant_record(session_id="s", message_id="m"))
    path.write_text(f"not json\n{good}\n")
    reader = IncrementalTranscript(path)
    reader.refresh()
    assert reader.stats.assistant_messages == 1


def test_metadata_and_counters(tmp_path):
    path = tmp_path / "s.jsonl"
    write_jsonl(
        path,
        [
            user_record("s", 0),
            {"type": "user", "sessionId": "s", "isMeta": True, "message": {"role": "user"}},
            {
                "type": "user",
                "sessionId": "s",
                "toolUseResult": {"ok": True},
                "message": {"role": "user"},
            },
            assistant_record(
                session_id="s",
                message_id="m1",
                blocks=[{"type": "tool_use", "name": "Task", "input": {"subagent_type": "Plan"}}],
                offset=1,
            ),
            {"type": "ai-title", "sessionId": "s", "aiTitle": "A title"},
            {"type": "custom-title", "sessionId": "s", "customTitle": "My title"},
            {"type": "agent-name", "sessionId": "s", "agentName": "nice-name"},
            {"type": "last-prompt", "sessionId": "s", "lastPrompt": "the prompt"},
            {"type": "permission-mode", "sessionId": "s", "permissionMode": "plan"},
        ],
    )
    reader = IncrementalTranscript(path)
    reader.refresh()
    stats = reader.stats

    assert stats.user_messages == 1, "meta records and tool results are not user turns"
    assert stats.spawn_count == 1
    assert stats.title == "A title"
    assert stats.custom_title == "My title"
    assert stats.agent_name == "nice-name"
    assert stats.last_prompt == "the prompt"
    assert stats.permission_mode == "plan"
    assert stats.git_branch == "main"
    assert stats.efforts["high"] == 1


def test_activity_buckets_by_hour(tmp_path):
    path = tmp_path / "s.jsonl"
    write_jsonl(
        path,
        [
            assistant_record(session_id="s", message_id="m1", offset=0),
            assistant_record(session_id="s", message_id="m2", offset=60),
            assistant_record(session_id="s", message_id="m3", offset=3600 * 2),
        ],
    )
    reader = IncrementalTranscript(path)
    reader.refresh()
    buckets = reader.stats.activity
    assert len(buckets) == 2
    assert sorted(count for count, _ in buckets.values()) == [1, 2]


def test_parse_timestamp():
    assert parse_timestamp("2026-08-31T12:00:00.000Z").hour == 12
    assert parse_timestamp(None) is None
    assert parse_timestamp("nonsense") is None

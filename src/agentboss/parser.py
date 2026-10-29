"""Incremental JSONL transcript parsing.

Transcripts are append-only and can reach tens of megabytes, so re-reading them
on every dashboard poll is not an option.  `IncrementalTranscript` keeps a
byte offset plus a running `TranscriptStats` and only parses the bytes that
were appended since the last look.  If the file is truncated or replaced the
offset is dropped and the file is re-read from the start.

Two things about the on-disk format matter for correctness:

* One API turn is written as *several* `assistant` records -- one per content
  block (thinking / text / tool_use) -- and every one of them repeats the same
  `message.usage`.  Counting each record would multiply tokens by the number of
  blocks, so usage is credited once per `message.id`.
* The tail of a live transcript is often a partial line.  Only whole lines are
  consumed; the offset stops at the last newline.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .quota import Spend, bucket_of, turn_cost

#: How many recent message ids to remember when de-duplicating usage.  Records
#: for one turn are written contiguously, so this only has to survive interleaving.
_RECENT_IDS = 64

_SYNTHETIC_MODELS = {"<synthetic>", "synthetic", ""}


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse Claude Code's ISO-8601 timestamps (`...Z`) into aware datetimes."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass
class ModelAccum:
    """Per-model counters."""

    messages: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    thinking: int = 0


@dataclass
class TranscriptStats:
    """Everything one transcript file contributes to the dashboard."""

    # Identity / metadata, last-write-wins as the transcript progresses.
    session_id: str | None = None
    agent_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    entrypoint: str | None = None
    title: str | None = None
    custom_title: str | None = None
    agent_name: str | None = None
    last_prompt: str | None = None
    permission_mode: str | None = None
    mode: str | None = None

    # Counters.
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    spawn_count: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    efforts: Counter[str] = field(default_factory=Counter)
    models: dict[str, ModelAccum] = field(default_factory=dict)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_tokens: int = 0

    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None

    #: hour-truncated UTC start -> [assistant turns, tokens]
    activity: dict[datetime, list[int]] = field(default_factory=dict)

    #: (five-minute bucket, model) -> spend, for quota windows.
    spend: dict[tuple[datetime, str], Spend] = field(default_factory=dict)
    #: Raw `quotaLimits` objects from rejected requests, with their timestamp.
    limit_hits: list[tuple[datetime, dict]] = field(default_factory=list)

    def model_accum(self, name: str) -> ModelAccum:
        accum = self.models.get(name)
        if accum is None:
            accum = self.models[name] = ModelAccum()
        return accum

    def note_time(self, when: datetime | None) -> None:
        if when is None:
            return
        if self.first_timestamp is None or when < self.first_timestamp:
            self.first_timestamp = when
        if self.last_timestamp is None or when > self.last_timestamp:
            self.last_timestamp = when


class IncrementalTranscript:
    """A transcript file plus the offset we have already digested."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stats = TranscriptStats()
        self._offset = 0
        self._inode: int | None = None
        self._recent_ids: deque[str] = deque(maxlen=_RECENT_IDS)
        self._recent_id_set: set[str] = set()
        self.size = 0
        self.mtime = 0.0
        self.lines_parsed = 0

    # -- public API ---------------------------------------------------------

    def refresh(self) -> bool:
        """Digest any newly appended bytes.  Returns True if anything changed."""
        try:
            stat = self.path.stat()
        except OSError:
            return False

        if stat.st_ino != self._inode or stat.st_size < self._offset:
            # New file, or truncated/rewritten: everything we knew is suspect.
            self._reset()
            self._inode = stat.st_ino

        self.mtime = stat.st_mtime
        if stat.st_size == self._offset:
            self.size = stat.st_size
            return False

        self.size = stat.st_size
        self._consume()
        return True

    # -- internals ----------------------------------------------------------

    def _reset(self) -> None:
        self.stats = TranscriptStats()
        self._offset = 0
        self._recent_ids.clear()
        self._recent_id_set.clear()
        self.lines_parsed = 0

    def _consume(self) -> None:
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()

        # Stop at the last complete line; the writer may be mid-record.
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return
        complete, self._offset = chunk[: cut + 1], self._offset + cut + 1

        for raw in complete.splitlines():
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                self._apply(record)
                self.lines_parsed += 1

    def _seen_message(self, message_id: str | None) -> bool:
        """True if this API turn's usage has already been credited."""
        if not message_id:
            return False
        if message_id in self._recent_id_set:
            return True
        if len(self._recent_ids) == self._recent_ids.maxlen:
            self._recent_id_set.discard(self._recent_ids[0])
        self._recent_ids.append(message_id)
        self._recent_id_set.add(message_id)
        return False

    def _apply(self, record: dict) -> None:
        stats = self.stats
        kind = record.get("type")

        if sid := record.get("sessionId"):
            stats.session_id = sid
        if aid := record.get("agentId"):
            stats.agent_id = aid

        when = parse_timestamp(record.get("timestamp"))
        stats.note_time(when)

        for key, attr in (
            ("cwd", "cwd"),
            ("gitBranch", "git_branch"),
            ("version", "version"),
            ("entrypoint", "entrypoint"),
        ):
            if value := record.get(key):
                setattr(stats, attr, value)

        if kind == "assistant":
            self._apply_assistant(record, when)
        elif kind == "user":
            # Tool results are replayed as user records; only real turns count.
            if not record.get("isMeta") and not record.get("toolUseResult"):
                stats.user_messages += 1
        elif kind == "ai-title":
            stats.title = record.get("aiTitle") or stats.title
        elif kind == "custom-title":
            stats.custom_title = record.get("customTitle") or stats.custom_title
        elif kind == "agent-name":
            stats.agent_name = record.get("agentName") or stats.agent_name
        elif kind == "last-prompt":
            stats.last_prompt = record.get("lastPrompt") or stats.last_prompt
        elif kind == "permission-mode":
            stats.permission_mode = record.get("permissionMode") or stats.permission_mode
        elif kind == "mode":
            stats.mode = record.get("mode") or stats.mode

    def _apply_assistant(self, record: dict, when: datetime | None) -> None:
        stats = self.stats
        message = record.get("message")
        if not isinstance(message, dict):
            return

        model = message.get("model")
        quota = record.get("quotaLimits")
        if isinstance(quota, dict) and when is not None and quota.get("status") == "rejected":
            stats.limit_hits.append((when, quota))

        first_block_of_turn = not self._seen_message(message.get("id"))

        # Tool calls are per-record: each content block appears exactly once.
        for block in message.get("content") or ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or "unknown"
            stats.tool_calls += 1
            stats.tools[name] += 1
            if name in ("Task", "Agent"):
                stats.spawn_count += 1

        if not first_block_of_turn:
            return

        # Usage and turn counts are per API turn, credited on its first record.
        stats.assistant_messages += 1
        if effort := record.get("effort"):
            stats.efforts[str(effort)] += 1

        usage = message.get("usage") or {}
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        details = usage.get("output_tokens_details") or {}
        thinking = int(details.get("thinking_tokens") or 0)

        stats.input_tokens += inp
        stats.output_tokens += out
        stats.cache_read_tokens += cache_read
        stats.cache_creation_tokens += cache_creation
        stats.thinking_tokens += thinking

        if model and model not in _SYNTHETIC_MODELS:
            accum = stats.model_accum(model)
            accum.messages += 1
            accum.input += inp
            accum.output += out
            accum.cache_read += cache_read
            accum.cache_creation += cache_creation
            accum.thinking += thinking

        if when is not None:
            bucket = when.replace(minute=0, second=0, microsecond=0)
            slot = stats.activity.get(bucket)
            if slot is None:
                slot = stats.activity[bucket] = [0, 0]
            slot[0] += 1
            slot[1] += inp + out + cache_read + cache_creation

            if model and model not in _SYNTHETIC_MODELS:
                key = (bucket_of(when), model)
                cell = stats.spend.get(key)
                if cell is None:
                    cell = stats.spend[key] = Spend()
                cell.turns += 1
                cell.tokens += inp + out + cache_read + cache_creation
                cell.cost += turn_cost(model, inp, out, cache_read, cache_creation)


def hour_buckets(hours: int, *, now: datetime | None = None) -> list[datetime]:
    """The last `hours` hour-starts, oldest first, for the activity chart."""
    end = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    return [end - timedelta(hours=offset) for offset in range(hours - 1, -1, -1)]


def read_json(path: Path) -> dict | None:
    """Read a small JSON file, tolerating races with the writer."""
    try:
        with path.open("rb") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

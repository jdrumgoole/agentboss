"""API schema.

These are the shapes the dashboard and the JSON API both speak.  Token counters
are *cumulative billed* tokens -- the sum over every assistant turn -- not the
size of the context window at any moment.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class SessionStatus(StrEnum):
    """How alive a session is."""

    BUSY = "busy"        # registered, process alive, currently working
    IDLE = "idle"        # registered, process alive, waiting on you
    STALE = "stale"      # registry entry exists but the process is gone
    ENDED = "ended"      # transcript on disk, no registry entry


LIVE_STATUSES = frozenset({SessionStatus.BUSY, SessionStatus.IDLE})


class TokenCounts(BaseModel):
    """Cumulative billed tokens."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    thinking: int = 0

    @computed_field
    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation

    def add(self, other: TokenCounts) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_creation += other.cache_creation
        self.thinking += other.thinking


class ModelUsage(BaseModel):
    """One model's share of a session, an agent, or the whole machine."""

    model: str
    messages: int = 0
    tokens: TokenCounts = Field(default_factory=TokenCounts)


class AgentSummary(BaseModel):
    """A subagent spawned by a session (Task/Agent tool, or a background skill)."""

    agent_id: str
    session_id: str
    project: str
    agent_type: str
    #: The agent definition it runs.  Differs from `agent_type` for named
    #: teammates, whose type is their name.
    base_type: str | None = None
    #: How it was spawned, e.g. `in_process_teammate`.
    task_kind: str | None = None
    #: The model requested at spawn time, if one was.
    requested_model: str | None = None
    description: str = ""
    spawn_depth: int = 1
    tool_use_id: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None

    messages: int = 0
    tool_calls: int = 0
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    models: list[ModelUsage] = Field(default_factory=list)
    tokens: TokenCounts = Field(default_factory=TokenCounts)

    @computed_field
    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock time between the agent's first and last recorded turn."""
        if self.started_at and self.last_activity_at:
            return (self.last_activity_at - self.started_at).total_seconds()
        return None


class SessionSummary(BaseModel):
    """Everything agentboss knows about one Claude Code session."""

    session_id: str
    project: str
    project_slug: str
    status: SessionStatus = SessionStatus.ENDED

    # From the live registry (~/.claude/sessions/<pid>.json), when present.
    name: str | None = None
    pid: int | None = None
    kind: str | None = None
    entrypoint: str | None = None
    socket_path: str | None = None
    #: The CLI's own word for what it is doing right now ("busy", "shell", "idle").
    activity_state: str | None = None

    # From the transcript.
    title: str | None = None
    last_prompt: str | None = None
    version: str | None = None
    git_branch: str | None = None
    permission_mode: str | None = None

    started_at: datetime | None = None
    last_activity_at: datetime | None = None

    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tools: dict[str, int] = Field(default_factory=dict)
    efforts: dict[str, int] = Field(default_factory=dict)

    models: list[ModelUsage] = Field(default_factory=list)
    #: Tokens billed to this session's main loop.
    tokens: TokenCounts = Field(default_factory=TokenCounts)
    #: Tokens billed to subagents this session spawned.
    agent_tokens: TokenCounts = Field(default_factory=TokenCounts)

    #: Subagents spawned directly by this session's main loop.
    agents: list[AgentSummary] = Field(default_factory=list)
    #: Subagent spawns recorded in the transcript, including ones with no
    #: transcript file of their own yet (still starting up, or run in-process).
    spawn_count: int = 0
    open_tasks: int = 0
    transcript_bytes: int = 0

    @computed_field
    @property
    def agent_count(self) -> int:
        return len(self.agents)

    @computed_field
    @property
    def subagent_count(self) -> int:
        """Agents nested below a first-level agent (spawn depth > 1)."""
        return sum(1 for a in self.agents if a.spawn_depth > 1)

    @computed_field
    @property
    def max_spawn_depth(self) -> int:
        return max((a.spawn_depth for a in self.agents), default=0)

    @computed_field
    @property
    def combined_tokens(self) -> int:
        """Main-loop tokens plus every subagent's."""
        return self.tokens.total + self.agent_tokens.total

    @computed_field
    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @computed_field
    @property
    def resume_command(self) -> str:
        return f"claude --resume {self.session_id}"


class ProjectSummary(BaseModel):
    """Sessions grouped by working directory."""

    project: str
    project_slug: str
    sessions: int = 0
    live_sessions: int = 0
    agents: int = 0
    tokens: TokenCounts = Field(default_factory=TokenCounts)
    last_activity_at: datetime | None = None


class ActivityBucket(BaseModel):
    """Assistant turns in one time bucket, for the activity chart."""

    start: datetime
    messages: int = 0
    tokens: int = 0


class Overview(BaseModel):
    """The dashboard's top-level numbers."""

    generated_at: datetime
    claude_home: str
    scan_seconds: float = 0.0

    total_sessions: int = 0
    live_sessions: int = 0
    busy_sessions: int = 0
    idle_sessions: int = 0
    total_agents: int = 0
    live_agents: int = 0
    subagents: int = 0
    max_spawn_depth: int = 0
    total_messages: int = 0
    total_tool_calls: int = 0

    tokens: TokenCounts = Field(default_factory=TokenCounts)
    models: list[ModelUsage] = Field(default_factory=list)
    agent_types: dict[str, int] = Field(default_factory=dict)
    projects: list[ProjectSummary] = Field(default_factory=list)
    activity: list[ActivityBucket] = Field(default_factory=list)


class LimitHit(BaseModel):
    """A request the plan's quota refused, as recorded in a transcript."""

    at: datetime
    limit_type: str
    status: str = "rejected"
    resets_at: datetime | None = None
    overage_status: str | None = None
    session_id: str | None = None
    #: Rejections recorded in this window, retries included.
    attempts: int = 0
    #: API-equivalent USD spent in the window when the rejection landed.
    spend_at_hit: float | None = None


class QuotaWindow(BaseModel):
    """Spend inside one limit window (a five-hour block or a seven-day week)."""

    kind: str
    start: datetime
    end: datetime
    active: bool = False
    #: API-equivalent USD -- a proxy for quota, not a bill.
    cost: float = 0.0
    tokens: int = 0
    turns: int = 0
    models: dict[str, float] = Field(default_factory=dict)
    #: Spend at the most recent recorded rejection of this kind, if any.
    estimated_cap: float | None = None
    used_fraction: float | None = None
    #: `cost` extrapolated to the end of the window at the current rate.
    projected_cost: float = 0.0


class SpendBucket(BaseModel):
    start: datetime
    cost: float = 0.0
    tokens: int = 0
    models: dict[str, float] = Field(default_factory=dict)


class QuotaReport(BaseModel):
    generated_at: datetime
    five_hour: QuotaWindow
    seven_day: QuotaWindow
    limit_hits: list[LimitHit] = Field(default_factory=list)
    daily: list[SpendBucket] = Field(default_factory=list)
    blocks: list[QuotaWindow] = Field(default_factory=list)


class Recommendation(BaseModel):
    """A concrete way to spend less quota, with the evidence behind it."""

    kind: str
    severity: str  # high | medium | low
    title: str
    detail: str
    session_id: str | None = None
    #: API-equivalent USD, when a saving can be estimated.
    estimated_savings: float | None = None
    savings_basis: str | None = None

"""Recommendations for spending less quota.

Every rule reads what the transcripts already record and names a concrete
action.  Where a saving can be estimated it is given in the same API-equivalent
dollars as the quota panel, with the basis spelled out; where it can't (how
long a large tool result stays in context depends on what you do next), the
recommendation says how much context is involved instead.

The rules, in the order the evidence usually ranks them:

* `context` -- a recent session re-reads a very large context on every turn.
  Cache reads are cheap per token but are paid on every turn, so context depth
  dominates most bills.
* `window` -- one session holds a large share of the current seven-day window.
* `subagent_model` -- an agent type runs on a premium model; the same work on
  Sonnet costs a fraction.
* `tool_output` -- a tool's results are routinely large, and each one is
  re-read on every later turn of the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import AgentSummary, Recommendation, SessionSummary
from .parser import TranscriptStats
from .quota import price_of

#: Context a turn is given once a session has been compacted or cleared.
LEAN_CONTEXT = 40_000
#: Above this, a recent session gets a compact/clear recommendation...
CONTEXT_WARN = 150_000
#: ...and above this it is ranked high.
CONTEXT_HIGH = 400_000
#: Sessions idle longer than this are history, not something to act on.
RECENT = timedelta(hours=24)

#: Share of the seven-day window one session must hold to be called out.
WINDOW_SHARE = 0.25
WINDOW_MIN_COST = 50.0

#: What a premium-model agent type would cost on Sonnet, and the floor for
#: bothering to say so.
CHEAPER_MODEL = "claude-sonnet-5-5"
SUBAGENT_MIN_SAVING = 10.0
SUBAGENT_LOOKBACK = timedelta(days=30)

#: A tool is flagged when its results average this many tokens, over at least
#: this many calls in the last week.  Characters are converted at ~4 per token.
TOOL_TOKENS_WARN = 5_000
TOOL_MIN_CALLS = 5
CHARS_PER_TOKEN = 4

TOOL_TIPS = {
    "Read": "Read the part you need with offset and limit instead of whole files.",
    "Bash": "Trim command output with head, tail or grep, and run noisy builds and "
    "tests quietly (-q) so only failures come back.",
    "Grep": "Narrow the pattern or path, cap matches with head_limit, or list "
    "matching files first and read only the ones that matter.",
    "WebFetch": "Ask for the specific facts you need from the page rather than its whole text.",
    "Agent": "Ask subagents for a short conclusion, not a dump of what they read.",
    "Task": "Ask subagents for a short conclusion, not a dump of what they read.",
}
GENERIC_TOOL_TIP = "Ask for less output, or filter it, so less of it lands in context."

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class SessionData:
    """One session and the raw transcript stats behind it."""

    summary: SessionSummary
    main: TranscriptStats
    agents: list[tuple[AgentSummary, TranscriptStats]] = field(default_factory=list)

    @property
    def label(self) -> str:
        s = self.summary
        return s.name or s.title or s.session_id[:8]

    def spend(self, start: datetime, end: datetime) -> float:
        total = 0.0
        for stats in (self.main, *(a[1] for a in self.agents)):
            total += sum(c.cost for (b, _), c in stats.spend.items() if start <= b < end)
        return total


def _cache_read_price(model: str | None) -> float:
    return price_of(model)[0] * 0.1 / 1e6


def _main_model(data: SessionData) -> str | None:
    """The model the session's main loop used most recently."""
    latest = max(data.main.spend, default=None)
    if latest is not None:
        return latest[1]
    s = data.summary
    return s.models[0].model if s.models else None


def context_rules(sessions: list[SessionData], now: datetime) -> list[Recommendation]:
    recs = []
    for data in sessions:
        s = data.summary
        ctx = data.main.context_tokens
        recent = s.is_live or (s.last_activity_at and now - s.last_activity_at < RECENT)
        if not recent or ctx < CONTEXT_WARN:
            continue
        per_100 = (ctx - LEAN_CONTEXT) * _cache_read_price(_main_model(data)) * 100
        recs.append(
            Recommendation(
                id=f"context:{s.session_id}",
                kind="context",
                severity="high" if ctx >= CONTEXT_HIGH else "medium",
                title=f"Compact or clear “{data.label}”",
                detail=(
                    f"Each turn re-reads about {ctx / 1000:,.0f}k tokens of context. "
                    "Run /compact to keep going on the same task, or /clear before "
                    f"starting an unrelated one; that brings each turn near "
                    f"{LEAN_CONTEXT // 1000}k."
                ),
                session_id=s.session_id,
                estimated_savings=round(per_100, 2),
                savings_basis="per 100 further turns",
            )
        )
    return recs


def window_rules(
    sessions: list[SessionData], week: tuple[datetime, datetime]
) -> list[Recommendation]:
    spends = [(d, d.spend(*week)) for d in sessions]
    total = sum(cost for _, cost in spends)
    if total <= 0:
        return []
    recs = []
    for data, cost in sorted(spends, key=lambda x: -x[1])[:3]:
        share = cost / total
        if share < WINDOW_SHARE or cost < WINDOW_MIN_COST:
            continue
        agent_cost = sum(
            sum(c.cost for (b, _), c in stats.spend.items() if week[0] <= b < week[1])
            for _, stats in data.agents
        )
        split = f" Subagents account for {agent_cost / cost:.0%} of it." if agent_cost else ""
        recs.append(
            Recommendation(
                id=f"window:{data.summary.session_id}",
                kind="window",
                severity="high" if share >= 0.5 else "medium",
                title=f"“{data.label}” has used {share:.0%} of this week's usage",
                detail=(
                    f"${cost:,.0f} API-equivalent since the window opened.{split} "
                    "If it is still running, it is the first place to compact, clear "
                    "or move work to a cheaper model."
                ),
                session_id=data.summary.session_id,
            )
        )
    return recs


def subagent_rules(sessions: list[SessionData], now: datetime) -> list[Recommendation]:
    since = now - SUBAGENT_LOOKBACK
    cheap_in = price_of(CHEAPER_MODEL)[0]

    @dataclass
    class Group:
        cost: float = 0.0
        saving: float = 0.0
        names: list[str] = field(default_factory=list)
        explicit: int = 0  # spawned with a model named

    # Named teammates are grouped under the definition they run, not their name.
    groups: dict[tuple[str, bool], Group] = {}
    for data in sessions:
        for agent, stats in data.agents:
            cost = saving = 0.0
            for (bucket, model), cell in stats.spend.items():
                if bucket < since:
                    continue
                price_in = price_of(model)[0]
                if price_in <= cheap_in:
                    continue
                cost += cell.cost
                saving += cell.cost * (1 - cheap_in / price_in)
            if not cost:
                continue
            teammate = agent.task_kind == "in_process_teammate"
            # Older teammates record only their name; group them together.
            if teammate:
                named_only = agent.base_type in (None, agent.agent_type)
                base = "" if named_only else agent.base_type
            else:
                base = agent.base_type or agent.agent_type
            group = groups.setdefault((base, teammate), Group())
            group.cost += cost
            group.saving += saving
            group.names.append(agent.agent_type)
            group.explicit += bool(agent.requested_model)

    recs = []
    for (base, teammate), g in groups.items():
        if g.saving < SUBAGENT_MIN_SAVING:
            continue
        count = len(g.names)
        what = f"{base} teammate".strip() if teammate else f"{base} agent"
        plural = "s" if count != 1 else ""
        if teammate:
            shown = ", ".join(sorted(set(g.names))[:4])
            more = "…" if len(set(g.names)) > 4 else ""
            subject = f"{count} {what}{plural} ({shown}{more})"
        else:
            subject = f"{count} {what}{plural}"
        if g.explicit:
            how = (
                ("It was" if count == 1 else f"{g.explicit} of them were")
                + " spawned with a premium model named explicitly; spawn "
                + ("it" if count == 1 else "them")
                + " with `sonnet` unless the task needs more."
            )
        elif base in {"general-purpose", "Explore", "Plan"}:
            how = "Ask for a cheaper model when delegating; the Agent tool takes a model argument."
        else:
            how = f"Set `model: sonnet` in the {base} agent definition."
        recs.append(
            Recommendation(
                id=f"subagent_model:{what}",
                kind="subagent_model",
                severity="medium" if g.saving >= 100 else "low",
                title=f"Run {what}s on Sonnet",
                detail=(
                    f"{subject} ran on a premium model in the last 30 days, "
                    f"${g.cost:,.0f} API-equivalent. {how} "
                    "Keep the premium model for work that needs it."
                ),
                estimated_savings=round(g.saving, 2),
                savings_basis="over the last 30 days, at Sonnet 5.5 prices",
            )
        )
    return recs


def tool_rules(sessions: list[SessionData], now: datetime) -> list[Recommendation]:
    chars: dict[str, int] = {}
    calls: dict[str, int] = {}
    since = now - timedelta(days=7)
    for data in sessions:
        last = data.summary.last_activity_at
        if not last or last < since:
            continue
        for stats in (data.main, *(a[1] for a in data.agents)):
            for name, size in stats.tool_result_chars.items():
                chars[name] = chars.get(name, 0) + size
                calls[name] = calls.get(name, 0) + stats.tool_result_count[name]

    recs = []
    for name, total in chars.items():
        count = calls[name]
        avg_tokens = total / count / CHARS_PER_TOKEN if count else 0
        if count < TOOL_MIN_CALLS or avg_tokens < TOOL_TOKENS_WARN:
            continue
        recs.append(
            Recommendation(
                id=f"tool_output:{name}",
                kind="tool_output",
                severity="medium" if avg_tokens >= 4 * TOOL_TOKENS_WARN else "low",
                title=f"{name} results are large",
                detail=(
                    f"{count:,} {name} calls in recently active sessions averaged about "
                    f"{avg_tokens / 1000:,.1f}k tokens each "
                    f"({total / CHARS_PER_TOKEN / 1e6:,.1f}M in all). Each result stays "
                    f"in context and is re-read on every later turn. "
                    f"{TOOL_TIPS.get(name, GENERIC_TOOL_TIP)}"
                ),
            )
        )
    return recs


def build_recommendations(
    sessions: list[SessionData], *, week: tuple[datetime, datetime], now: datetime
) -> list[Recommendation]:
    recs = [
        *context_rules(sessions, now),
        *window_rules(sessions, week),
        *subagent_rules(sessions, now),
        *tool_rules(sessions, now),
    ]
    recs.sort(key=lambda r: (_SEVERITY_ORDER[r.severity], -(r.estimated_savings or 0)))
    return recs

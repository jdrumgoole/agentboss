"""The scanner: turn the on-disk Claude Code state into dashboard objects.

One `SessionStore` owns every open transcript reader for the process.  A
scan re-stats the tree, digests whatever was appended, and rebuilds the summary
objects; readers persist across scans so the expensive part (parsing) is only
ever done once per byte.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings, decode_project_slug
from .models import (
    ActivityBucket,
    AgentSummary,
    LimitHit,
    ModelUsage,
    Overview,
    ProjectSummary,
    QuotaReport,
    Recommendation,
    SessionStatus,
    SessionSummary,
    TokenCounts,
)
from .parser import (
    IncrementalTranscript,
    ModelAccum,
    TranscriptStats,
    hour_buckets,
    read_json,
)
from .quota import Spend, build_report
from .recommend import SessionData, build_recommendations
from .registry import RegistryEntry, open_tasks, read_registry

SORT_KEYS = {
    "last_activity",
    "started",
    "tokens",
    "messages",
    "agents",
    "project",
    "status",
}

_STATUS_ORDER = {
    SessionStatus.BUSY: 0,
    SessionStatus.IDLE: 1,
    SessionStatus.STALE: 2,
    SessionStatus.ENDED: 3,
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: A scan requested marginally before the interval elapses still counts as due.
#: Callers pace themselves by sleeping for exactly `refresh_interval`, and
#: scheduling jitter would otherwise push every other tick into the next cycle.
_SCAN_DUE_TOLERANCE = 0.9


def _tokens_from(stats: TranscriptStats) -> TokenCounts:
    return TokenCounts(
        input=stats.input_tokens,
        output=stats.output_tokens,
        cache_read=stats.cache_read_tokens,
        cache_creation=stats.cache_creation_tokens,
        thinking=stats.thinking_tokens,
    )


def _model_usage(models: dict[str, ModelAccum]) -> list[ModelUsage]:
    usage = [
        ModelUsage(
            model=name,
            messages=accum.messages,
            tokens=TokenCounts(
                input=accum.input,
                output=accum.output,
                cache_read=accum.cache_read,
                cache_creation=accum.cache_creation,
                thinking=accum.thinking,
            ),
        )
        for name, accum in models.items()
    ]
    usage.sort(key=lambda item: (-item.tokens.total, item.model))
    return usage


class SessionStore:
    """Scans, caches and queries the Claude Code state tree."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self._lock = threading.RLock()
        self._transcripts: dict[Path, IncrementalTranscript] = {}
        self._agent_meta: dict[Path, tuple[float, dict]] = {}
        self._sessions: dict[str, SessionSummary] = {}
        #: session id -> its main transcript, for reaching the raw stats.
        self._session_paths: dict[str, Path] = {}
        self._last_scan = 0.0
        self._scan_seconds = 0.0
        self._generated_at = datetime.now(UTC)

    # -- scanning -----------------------------------------------------------

    def scan(self, *, force: bool = False) -> None:
        """Refresh the cache, at most once per `refresh_interval` unless forced."""
        with self._lock:
            now = time.monotonic()
            due_after = self.settings.refresh_interval * _SCAN_DUE_TOLERANCE
            if not force and (now - self._last_scan) < due_after:
                return
            started = time.perf_counter()
            self._rescan()
            self._scan_seconds = time.perf_counter() - started
            self._last_scan = time.monotonic()
            self._generated_at = datetime.now(UTC)

    def _rescan(self) -> None:
        registry = read_registry(self.settings.sessions_dir)
        sessions: dict[str, SessionSummary] = {}
        session_paths: dict[str, Path] = {}
        seen_paths: set[Path] = set()

        for project_dir in self._project_dirs():
            slug = project_dir.name
            for transcript_path in self._transcript_files(project_dir):
                seen_paths.add(transcript_path)
                session_id = transcript_path.stem
                reader = self._reader(transcript_path)
                reader.refresh()

                agents = self._scan_agents(project_dir, session_id, slug, seen_paths)
                summary = self._build_session(
                    session_id=session_id,
                    slug=slug,
                    reader=reader,
                    agents=agents,
                    entry=registry.get(session_id),
                )
                sessions[session_id] = summary
                session_paths[session_id] = transcript_path

        # A live session that has not written a transcript yet still deserves a row.
        for session_id, entry in registry.items():
            if session_id in sessions:
                continue
            sessions[session_id] = self._session_from_registry_only(session_id, entry)

        self._prune(seen_paths)
        self._sessions = sessions
        self._session_paths = session_paths

    def _project_dirs(self) -> list[Path]:
        root = self.settings.projects_dir
        if not root.is_dir():
            return []
        try:
            return [Path(entry.path) for entry in os.scandir(root) if entry.is_dir()]
        except OSError:
            return []

    @staticmethod
    def _transcript_files(project_dir: Path) -> list[Path]:
        try:
            return sorted(
                Path(entry.path)
                for entry in os.scandir(project_dir)
                if entry.is_file() and entry.name.endswith(".jsonl")
            )
        except OSError:
            return []

    def _reader(self, path: Path) -> IncrementalTranscript:
        reader = self._transcripts.get(path)
        if reader is None:
            reader = self._transcripts[path] = IncrementalTranscript(path)
        return reader

    def _prune(self, seen: set[Path]) -> None:
        for path in list(self._transcripts):
            if path not in seen:
                del self._transcripts[path]
        for path in list(self._agent_meta):
            if not path.exists():
                del self._agent_meta[path]

    # -- subagents ----------------------------------------------------------

    def _scan_agents(
        self,
        project_dir: Path,
        session_id: str,
        slug: str,
        seen_paths: set[Path],
    ) -> list[AgentSummary]:
        subagents_dir = project_dir / session_id / "subagents"
        if not subagents_dir.is_dir():
            return []

        agents: list[AgentSummary] = []
        try:
            entries = sorted(os.scandir(subagents_dir), key=lambda e: e.name)
        except OSError:
            return []

        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".jsonl"):
                continue
            path = Path(entry.path)
            seen_paths.add(path)
            reader = self._reader(path)
            reader.refresh()
            meta = self._agent_metadata(path.parent / f"{path.stem}.meta.json")
            agents.append(
                self._build_agent(
                    agent_id=path.stem,
                    session_id=session_id,
                    project=reader.stats.cwd or decode_project_slug(slug),
                    reader=reader,
                    meta=meta,
                )
            )

        agents.sort(key=lambda a: (a.started_at or _EPOCH, a.agent_id))
        return agents

    def _agent_metadata(self, path: Path) -> dict:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._agent_meta.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        data = read_json(path) or {}
        self._agent_meta[path] = (mtime, data)
        return data

    @staticmethod
    def _build_agent(
        *,
        agent_id: str,
        session_id: str,
        project: str,
        reader: IncrementalTranscript,
        meta: dict,
    ) -> AgentSummary:
        stats = reader.stats
        return AgentSummary(
            agent_id=agent_id,
            session_id=session_id,
            project=project,
            agent_type=str(meta.get("agentType") or "unknown"),
            base_type=meta.get("spawnedAgentType") or meta.get("agentType"),
            task_kind=meta.get("taskKind"),
            requested_model=meta.get("model"),
            description=str(meta.get("description") or ""),
            spawn_depth=int(meta.get("spawnDepth") or 1),
            tool_use_id=meta.get("toolUseId"),
            worktree_path=meta.get("worktreePath"),
            worktree_branch=meta.get("worktreeBranch"),
            messages=stats.assistant_messages,
            tool_calls=stats.tool_calls,
            started_at=stats.first_timestamp,
            last_activity_at=stats.last_timestamp,
            models=_model_usage(stats.models),
            tokens=_tokens_from(stats),
        )

    # -- sessions -----------------------------------------------------------

    def _build_session(
        self,
        *,
        session_id: str,
        slug: str,
        reader: IncrementalTranscript,
        agents: list[AgentSummary],
        entry: RegistryEntry | None,
    ) -> SessionSummary:
        stats = reader.stats
        project = stats.cwd or (entry.cwd if entry else None) or decode_project_slug(slug)

        agent_tokens = TokenCounts()
        for agent in agents:
            agent_tokens.add(agent.tokens)

        # A live session that has been quiet still counts as active as of the
        # last time the CLI touched its registry file.
        last_activity = stats.last_timestamp
        if entry and entry.updated_at:
            if last_activity is None or entry.updated_at > last_activity:
                last_activity = entry.updated_at

        return SessionSummary(
            session_id=session_id,
            project=project,
            project_slug=slug,
            status=entry.status if entry else SessionStatus.ENDED,
            name=(entry.name if entry else None) or stats.agent_name,
            pid=entry.pid if entry else None,
            kind=entry.kind if entry else None,
            entrypoint=(entry.entrypoint if entry else None) or stats.entrypoint,
            socket_path=entry.socket_path if entry else None,
            activity_state=entry.reported_status if entry else None,
            title=stats.custom_title or stats.title,
            last_prompt=stats.last_prompt,
            version=stats.version or (entry.version if entry else None),
            git_branch=stats.git_branch,
            permission_mode=stats.permission_mode or stats.mode,
            started_at=stats.first_timestamp or (entry.started_at if entry else None),
            last_activity_at=last_activity,
            user_messages=stats.user_messages,
            assistant_messages=stats.assistant_messages,
            tool_calls=stats.tool_calls,
            tools=dict(stats.tools.most_common()),
            efforts=dict(stats.efforts),
            models=_model_usage(stats.models),
            tokens=_tokens_from(stats),
            agent_tokens=agent_tokens,
            agents=agents,
            spawn_count=stats.spawn_count,
            open_tasks=open_tasks(self.settings.tasks_dir, session_id),
            transcript_bytes=reader.size,
        )

    def _session_from_registry_only(
        self, session_id: str, entry: RegistryEntry
    ) -> SessionSummary:
        project = entry.cwd or "unknown"
        return SessionSummary(
            session_id=session_id,
            project=project,
            project_slug=project,
            status=entry.status,
            name=entry.name,
            pid=entry.pid,
            kind=entry.kind,
            entrypoint=entry.entrypoint,
            socket_path=entry.socket_path,
            activity_state=entry.reported_status,
            version=entry.version,
            started_at=entry.started_at,
            last_activity_at=entry.updated_at,
            open_tasks=open_tasks(self.settings.tasks_dir, session_id),
        )

    # -- queries ------------------------------------------------------------

    def all_sessions(self) -> list[SessionSummary]:
        self.scan()
        with self._lock:
            return list(self._sessions.values())

    def get_session(self, session_id: str) -> SessionSummary | None:
        self.scan()
        with self._lock:
            return self._sessions.get(session_id)

    def query_sessions(
        self,
        *,
        status: str | None = None,
        project: str | None = None,
        model: str | None = None,
        agent_type: str | None = None,
        search: str | None = None,
        sort: str = "last_activity",
        descending: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionSummary]:
        """Filter and sort sessions.  `status='live'` means busy or idle."""
        sessions = self.all_sessions()

        if status and status != "all":
            wanted = {"busy", "idle"} if status == "live" else {status}
            sessions = [s for s in sessions if s.status.value in wanted]
        if project:
            sessions = [s for s in sessions if s.project == project]
        if model:
            sessions = [
                s
                for s in sessions
                if any(m.model == model for m in s.models)
                or any(m.model == model for a in s.agents for m in a.models)
            ]
        if agent_type:
            sessions = [s for s in sessions if any(a.agent_type == agent_type for a in s.agents)]
        if search:
            needle = search.lower()
            sessions = [s for s in sessions if _matches(s, needle)]

        sessions.sort(key=_sort_key(sort), reverse=descending)
        if offset:
            sessions = sessions[offset:]
        if limit is not None:
            sessions = sessions[:limit]
        return sessions

    def query_agents(
        self,
        *,
        session_id: str | None = None,
        agent_type: str | None = None,
        model: str | None = None,
        live_only: bool = False,
        limit: int | None = None,
    ) -> list[AgentSummary]:
        self.scan()
        with self._lock:
            sessions = list(self._sessions.values())

        agents: list[AgentSummary] = []
        for session in sessions:
            if session_id and session.session_id != session_id:
                continue
            if live_only and not session.is_live:
                continue
            agents.extend(session.agents)

        if agent_type:
            agents = [a for a in agents if a.agent_type == agent_type]
        if model:
            agents = [a for a in agents if any(m.model == model for m in a.models)]

        agents.sort(key=lambda a: a.last_activity_at or _EPOCH, reverse=True)
        return agents[:limit] if limit is not None else agents

    def overview(self, *, hours: int = 24) -> Overview:
        """The dashboard's headline numbers, model mix and activity timeline."""
        sessions = self.all_sessions()
        with self._lock:
            scan_seconds, generated_at = self._scan_seconds, self._generated_at

        overview = Overview(
            generated_at=generated_at,
            claude_home=str(self.settings.claude_home),
            scan_seconds=round(scan_seconds, 4),
            total_sessions=len(sessions),
        )

        models: dict[str, ModelUsage] = {}
        agent_types: dict[str, int] = {}
        projects: dict[str, ProjectSummary] = {}

        for session in sessions:
            if session.status is SessionStatus.BUSY:
                overview.busy_sessions += 1
            elif session.status is SessionStatus.IDLE:
                overview.idle_sessions += 1

            overview.total_messages += session.user_messages + session.assistant_messages
            overview.total_tool_calls += session.tool_calls
            overview.tokens.add(session.tokens)
            overview.tokens.add(session.agent_tokens)
            overview.total_agents += session.agent_count
            overview.subagents += session.subagent_count
            overview.max_spawn_depth = max(overview.max_spawn_depth, session.max_spawn_depth)
            if session.is_live:
                overview.live_agents += session.agent_count

            _merge_models(models, session.models)
            for agent in session.agents:
                agent_types[agent.agent_type] = agent_types.get(agent.agent_type, 0) + 1
                overview.total_messages += agent.messages
                overview.total_tool_calls += agent.tool_calls
                _merge_models(models, agent.models)

            project = projects.get(session.project)
            if project is None:
                project = projects[session.project] = ProjectSummary(
                    project=session.project, project_slug=session.project_slug
                )
            project.sessions += 1
            project.live_sessions += int(session.is_live)
            project.agents += session.agent_count
            project.tokens.add(session.tokens)
            project.tokens.add(session.agent_tokens)
            if session.last_activity_at and (
                project.last_activity_at is None
                or session.last_activity_at > project.last_activity_at
            ):
                project.last_activity_at = session.last_activity_at

        overview.live_sessions = overview.busy_sessions + overview.idle_sessions
        overview.models = sorted(
            models.values(), key=lambda m: (-m.tokens.total, m.model)
        )
        overview.agent_types = dict(
            sorted(agent_types.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        overview.projects = sorted(
            projects.values(),
            key=lambda p: p.last_activity_at or _EPOCH,
            reverse=True,
        )
        overview.activity = self.activity(hours=hours)
        return overview

    def activity(self, *, hours: int = 24) -> list[ActivityBucket]:
        """Assistant turns and tokens per hour, across every session and agent."""
        self.scan()
        buckets = hour_buckets(hours)
        window = {bucket: [0, 0] for bucket in buckets}
        cutoff = buckets[0]

        with self._lock:
            readers = list(self._transcripts.values())

        for reader in readers:
            for bucket, (messages, tokens) in reader.stats.activity.items():
                if bucket < cutoff:
                    continue
                slot = window.get(bucket)
                if slot is None:
                    continue
                slot[0] += messages
                slot[1] += tokens

        return [
            ActivityBucket(start=bucket, messages=window[bucket][0], tokens=window[bucket][1])
            for bucket in buckets
        ]

    def quota(self, *, days: int = 30) -> QuotaReport:
        """Spend per limit window, calibrated against recorded rejections."""
        self.scan()
        with self._lock:
            readers = list(self._transcripts.values())

        ledger: dict[tuple[datetime, str], Spend] = {}
        hits: dict[tuple[datetime, str], LimitHit] = {}
        for reader in readers:
            stats = reader.stats
            for key, cell in stats.spend.items():
                total = ledger.get(key)
                if total is None:
                    total = ledger[key] = Spend()
                total.turns += cell.turns
                total.tokens += cell.tokens
                total.cost += cell.cost
            for when, raw in stats.limit_hits:
                kind = str(raw.get("rateLimitType") or "unknown")
                resets = raw.get("resetsAt")
                resets_at = None
                if isinstance(resets, (int, float)):
                    resets_at = datetime.fromtimestamp(resets, UTC)
                # A refused request gets retried until the reset; keep the first
                # rejection per window -- that is the moment the cap was reached.
                key = (resets_at or when.replace(minute=0, second=0, microsecond=0), kind)
                hit = hits.get(key)
                if hit is None or when < hit.at:
                    attempts = hit.attempts if hit else 0
                    hit = hits[key] = LimitHit(
                        at=when,
                        limit_type=kind,
                        status=str(raw.get("status") or "rejected"),
                        resets_at=resets_at,
                        overage_status=raw.get("overageStatus"),
                        session_id=stats.session_id,
                        attempts=attempts,
                    )
                hit.attempts += 1
        return build_report(ledger, list(hits.values()), days=days)

    def recommendations(self) -> list[Recommendation]:
        """Ways to spend less quota, most urgent first."""
        week = self.quota(days=1).seven_day
        with self._lock:
            data = []
            for session_id, path in self._session_paths.items():
                reader = self._transcripts.get(path)
                summary = self._sessions.get(session_id)
                if reader is None or summary is None:
                    continue
                agents_dir = path.parent / session_id / "subagents"
                agents = [
                    (agent, self._transcripts[agents_dir / f"{agent.agent_id}.jsonl"].stats)
                    for agent in summary.agents
                    if agents_dir / f"{agent.agent_id}.jsonl" in self._transcripts
                ]
                data.append(SessionData(summary=summary, main=reader.stats, agents=agents))
        return build_recommendations(
            data, week=(week.start, week.end), now=datetime.now(UTC)
        )

    def known_models(self) -> list[str]:
        return [usage.model for usage in self.overview(hours=1).models]

    def known_projects(self) -> list[str]:
        return sorted({session.project for session in self.all_sessions()})

    def known_agent_types(self) -> list[str]:
        types: set[str] = set()
        for session in self.all_sessions():
            types.update(agent.agent_type for agent in session.agents)
        return sorted(types)


def _merge_models(target: dict[str, ModelUsage], source: list[ModelUsage]) -> None:
    for usage in source:
        existing = target.get(usage.model)
        if existing is None:
            target[usage.model] = ModelUsage(
                model=usage.model,
                messages=usage.messages,
                tokens=usage.tokens.model_copy(deep=True),
            )
        else:
            existing.messages += usage.messages
            existing.tokens.add(usage.tokens)


def _matches(session: SessionSummary, needle: str) -> bool:
    haystack = (
        session.session_id,
        session.project,
        session.name or "",
        session.title or "",
        session.last_prompt or "",
        session.git_branch or "",
    )
    return any(needle in field.lower() for field in haystack)


def _sort_key(sort: str):
    if sort not in SORT_KEYS:
        sort = "last_activity"
    if sort == "started":
        return lambda s: s.started_at or _EPOCH
    if sort == "tokens":
        return lambda s: s.combined_tokens
    if sort == "messages":
        return lambda s: s.user_messages + s.assistant_messages
    if sort == "agents":
        return lambda s: s.agent_count
    if sort == "project":
        return lambda s: s.project
    if sort == "status":
        return lambda s: -_STATUS_ORDER[s.status]
    return lambda s: s.last_activity_at or _EPOCH

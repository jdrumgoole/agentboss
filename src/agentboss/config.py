"""Locations and tunables.

Everything agentboss reads lives under a Claude Code home directory (``~/.claude``
by default).  The layout it depends on:

    ~/.claude/
      sessions/<pid>.json                      live process registry (one per running CLI)
      projects/<encoded-cwd>/
        <session-id>.jsonl                     main transcript
        <session-id>/subagents/
          agent-<id>.jsonl                     subagent transcript
          agent-<id>.meta.json                 subagent type / description / spawn depth
      tasks/<session-id>/<n>.json              task-list items
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CLAUDE_HOME = Path.home() / ".claude"

@dataclass(frozen=True)
class Settings:
    """Where to read from and how eagerly to re-read."""

    claude_home: Path = DEFAULT_CLAUDE_HOME
    #: Minimum seconds between filesystem rescans triggered by API requests.
    refresh_interval: float = 1.0
    #: Allow the control endpoints (interrupt / terminate a running session).
    allow_control: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        home = os.environ.get("AGENTBOSS_CLAUDE_HOME")
        return cls(
            claude_home=Path(home).expanduser() if home else DEFAULT_CLAUDE_HOME,
            refresh_interval=float(os.environ.get("AGENTBOSS_REFRESH_INTERVAL", "1.0")),
            allow_control=os.environ.get("AGENTBOSS_ALLOW_CONTROL", "").lower()
            in {"1", "true", "yes"},
        )

    @property
    def projects_dir(self) -> Path:
        return self.claude_home / "projects"

    @property
    def sessions_dir(self) -> Path:
        return self.claude_home / "sessions"

    @property
    def tasks_dir(self) -> Path:
        return self.claude_home / "tasks"


def decode_project_slug(slug: str) -> str:
    """Best-effort inverse of Claude Code's cwd -> directory-name encoding.

    Path separators become ``-``, which is lossy: a directory whose real name
    contains ``-`` is indistinguishable from a separator.  We therefore only use
    the decoded value as a display fallback -- the authoritative ``cwd`` comes
    from the transcript records themselves whenever one has been read.
    """
    if not slug.startswith("-"):
        return slug
    return "/" + slug[1:].replace("-", "/")

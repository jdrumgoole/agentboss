"""The live-session registry.

Every running Claude Code CLI drops ``~/.claude/sessions/<pid>.json`` and keeps a
``status`` field ("busy" / "idle") up to date.  That file is the only place the
*current* state of a session is recorded, but it outlives crashes: a stale entry
looks identical to a live one until you check the process.

Liveness here is deliberately conservative.  A pid alone is not enough -- pids get
recycled -- so the pid is also required to still *be* a Claude process, checked
against the command name the OS reports.  (The recorded ``procStart`` string is
not usable for this: ``ps -o lstart`` formats it per-locale and in a different
field order, and the two clocks disagree by an hour across a DST boundary.)  A
single ``ps`` call covers every session.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import SessionStatus
from .parser import read_json


@dataclass
class RegistryEntry:
    """One ``~/.claude/sessions/<pid>.json``, plus what we learned about the pid."""

    pid: int
    session_id: str
    cwd: str | None = None
    name: str | None = None
    version: str | None = None
    kind: str | None = None
    entrypoint: str | None = None
    socket_path: str | None = None
    reported_status: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    alive: bool = False

    @property
    def status(self) -> SessionStatus:
        """Live sessions report what the CLI last wrote; dead pids are stale.

        The CLI only rewrites its registry file when its status changes, so a
        quiet ``updatedAt`` means "still busy on one long tool call", not "gone".
        Liveness is therefore decided by the process, never by the clock.
        """
        if not self.alive:
            return SessionStatus.STALE
        # The CLI reports more than two states ("busy", "shell", "idle", ...).
        # Only "idle" means it is waiting on you; everything else is work.
        return SessionStatus.IDLE if self.reported_status == "idle" else SessionStatus.BUSY


def _epoch_ms(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


#: Command names a running Claude Code CLI may present as.
CLAUDE_COMMANDS = ("claude", "node", "bun")


def running_commands() -> dict[int, str]:
    """pid -> command name, for every process on the machine.

    Returns an empty mapping if ``ps`` is unavailable or unusable, in which case
    callers fall back to a plain pid-existence check.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}

    commands: dict[int, str] = {}
    for line in out.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        try:
            commands[int(pid_text)] = command.strip()
        except ValueError:
            continue
    return commands


def _looks_like_claude(command: str) -> bool:
    """True if this command name could be a Claude Code CLI.

    Deliberately permissive: a false positive only survives until the next scan,
    while a false negative would report a running session as dead.
    """
    name = command.rsplit("/", 1)[-1].lower()
    return any(candidate in name for candidate in CLAUDE_COMMANDS)


def read_registry(sessions_dir: Path) -> dict[str, RegistryEntry]:
    """Read every registry file, keyed by session id.

    When two pids claim the same session id (a resumed session whose predecessor
    left a stale file behind), the live one wins, then the most recently updated.
    """
    if not sessions_dir.is_dir():
        return {}

    commands = running_commands()
    entries: dict[str, RegistryEntry] = {}

    for path in sessions_dir.glob("*.json"):
        data = read_json(path)
        if not data:
            continue
        session_id = data.get("sessionId")
        pid = data.get("pid")
        if not isinstance(session_id, str) or not isinstance(pid, int):
            continue

        if commands:
            command = commands.get(pid)
            alive = command is not None and _looks_like_claude(command)
        else:
            alive = _pid_exists(pid)

        entry = RegistryEntry(
            pid=pid,
            session_id=session_id,
            cwd=data.get("cwd"),
            name=data.get("name"),
            version=data.get("version"),
            kind=data.get("kind"),
            entrypoint=data.get("entrypoint"),
            socket_path=data.get("messagingSocketPath"),
            reported_status=data.get("status"),
            started_at=_epoch_ms(data.get("startedAt")),
            updated_at=_epoch_ms(data.get("updatedAt")),
            alive=alive,
        )

        existing = entries.get(session_id)
        if existing is None or _prefer(entry, existing):
            entries[session_id] = entry

    return entries


def _prefer(candidate: RegistryEntry, incumbent: RegistryEntry) -> bool:
    if candidate.alive != incumbent.alive:
        return candidate.alive
    return (candidate.updated_at or datetime.min.replace(tzinfo=UTC)) > (
        incumbent.updated_at or datetime.min.replace(tzinfo=UTC)
    )


def open_tasks(tasks_dir: Path, session_id: str) -> int:
    """Count task-list items for a session that are not finished."""
    session_tasks = tasks_dir / session_id
    if not session_tasks.is_dir():
        return 0
    count = 0
    for path in session_tasks.glob("*.json"):
        data = read_json(path)
        if data and data.get("status") in {"pending", "in_progress"}:
            count += 1
    return count


def signal_session(pid: int, sig: int) -> None:
    """Send a signal to a session's process.  Raises OSError if it cannot."""
    os.kill(pid, sig)
    time.sleep(0)  # yield; the caller re-reads the registry on the next scan

"""Workspace sessions — the bookkeeping behind STAR 2.0's invisible background desktop.

Blueprint §8: *"The target is not simply minimizing a window. Star should have a
controlled execution workspace separate from the visible Star UI."* A session is
that controlled place: an identity, a jailed directory, a lifetime, checkpoints
and honest numbers about what is inside it.

This module is deliberately data-only (no I/O): :mod:`.manager` performs the work
and :mod:`.isolation` decides how strongly a session is separated from the
visible desktop.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ALIVE_STATES",
    "Checkpoint",
    "USABLE_STATES",
    "WorkspaceKind",
    "WorkspaceSession",
    "WorkspaceState",
    "new_session_id",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_session_id(prefix: str = "ws") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class WorkspaceState(str, Enum):
    """Lifecycle of a background session."""

    ACTIVE = "active"              # in use right now
    IDLE = "idle"                  # created, nothing running in it
    SUSPENDED = "suspended"        # checkpointed; work can resume later
    CLOSED = "closed"              # finished; files kept for inspection
    EXPIRED = "expired"            # TTL passed; closed by the manager
    DESTROYED = "destroyed"        # files removed (inside the jail only)
    ERROR = "error"                # something went wrong; see `error`


#: states that still hold a slot in the session budget
ALIVE_STATES = frozenset({WorkspaceState.ACTIVE, WorkspaceState.IDLE, WorkspaceState.SUSPENDED})
#: states a human can still act on (write, checkpoint, close)
USABLE_STATES = ALIVE_STATES


class WorkspaceKind(str, Enum):
    """What a session is for — the blueprint's background-workspace roles."""

    GENERIC = "generic"
    BROWSER = "browser"            # isolated browser/session profile
    COMPUTER = "computer"          # screen capture + motor scratch space
    FILES = "files"                # read/create/transform authorised files
    CODING = "coding"              # inspect/edit/test code
    SYSTEM = "system"              # approved application/system actions


class Checkpoint(BaseModel):
    """A restorable snapshot of a session's files."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    label: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    files: int = 0
    bytes: int = 0
    truncated: bool = False        # the snapshot hit a cap — say so, never lie
    path: str = ""                 # where the snapshot lives (inside the jail)
    note: str = ""

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class WorkspaceSession(BaseModel):
    """One isolated background execution session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=new_session_id)
    kind: WorkspaceKind = WorkspaceKind.GENERIC
    label: str = ""
    #: absolute path of the session's jailed directory
    root: Path
    state: WorkspaceState = WorkspaceState.IDLE
    isolation: str = "null"        # which isolation backend prepared this session
    dry_run: bool = True           # the development default: record, don't disturb
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = None
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    files: int = 0
    bytes: int = 0
    pid: int | None = None         # set only when work really runs in its own process
    agent: str = ""                # which agent asked for this session
    notes: list[str] = Field(default_factory=list)
    error: str | None = None

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return self.state in ALIVE_STATES

    @property
    def usable(self) -> bool:
        return self.state in USABLE_STATES

    @property
    def name(self) -> str:
        return self.label or f"{self.kind.value} session"

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None or not self.alive:
            return False
        return (now or _utcnow()) >= self.expires_at

    def touch(self, now: datetime | None = None) -> None:
        self.last_used_at = now or _utcnow()

    def note(self, message: str) -> None:
        """Append an honest remark (cap 24, each ≤ 200 chars)."""
        text = str(message or "").strip()[:200]
        if text:
            self.notes.append(text)
            del self.notes[:-24]

    def checkpoint_ids(self) -> list[str]:
        return [item.checkpoint_id for item in self.checkpoints]

    def public(self) -> dict[str, Any]:
        """JSON-safe view for the gateway, the console and the audit trail."""
        return {
            "session_id": self.session_id,
            "kind": self.kind.value,
            "label": self.label,
            "name": self.name,
            "path": str(self.root),
            "state": self.state.value,
            "isolation": self.isolation,
            "dry_run": self.dry_run,
            "agent": self.agent,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "checkpoints": len(self.checkpoints),
            "checkpoint_ids": self.checkpoint_ids(),
            "files": self.files,
            "bytes": self.bytes,
            "pid": self.pid,
            "notes": list(self.notes[-6:]),
            "error": self.error,
        }

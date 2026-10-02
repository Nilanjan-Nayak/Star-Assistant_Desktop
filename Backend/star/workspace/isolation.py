"""Isolation backends — how strongly a background session is separated from the visible Star.

Blueprint §8: *"For Windows, start with a workspace/session-manager abstraction. Do not
hard-code one virtual-desktop technology into every agent. The workspace can initially
isolate browser/app execution, then gain stronger desktop isolation as the implementation
matures."*

So this module is an **abstraction with honest capability flags**, not a promise:

============================  ===================================================================
backend                       what it actually does
============================  ===================================================================
``null`` (default)            the session is a jailed directory with quotas and checkpoints. No
                              OS-level separation is claimed — and ``describe()`` says so.
``process``                   same jail, plus the *declared* intent that the session's work runs
                              in its own OS process. ``spawn()`` exists but is opt-in
                              (``STAR_WORKSPACE_ALLOW_SPAWN``) and refuses in dry-run.
``windows_virtual_desktop``   Windows-only, and **not wired to any vendor API yet**: it reports
                              itself unavailable with a reason and falls back, which is exactly
                              the point of the abstraction — swapping in a real implementation
                              later must not touch a single agent.
============================  ===================================================================

The path jail itself is *always* enforced by :class:`~Backend.star.workspace.manager.WorkspaceManager`
— no backend can weaken it.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Protocol

from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger
from Backend.star.workspace.session import WorkspaceSession

__all__ = [
    "IsolationBackend",
    "NullIsolation",
    "ProcessIsolation",
    "VirtualDesktopIsolation",
    "build_isolation",
]

_log = star_logger("star2.workspace.isolation")


class IsolationBackend(Protocol):
    """What the manager needs from an isolation strategy."""

    name: str

    def available(self) -> bool: ...

    def describe(self) -> dict[str, Any]: ...

    def prepare(self, session: WorkspaceSession) -> None: ...

    def release(self, session: WorkspaceSession) -> None: ...


class NullIsolation:
    """Bookkeeping only: a jailed directory, quotas, checkpoints. No OS-level claims."""

    name = "null"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def available(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "available": True,
            "capabilities": {"jail": True, "quota": True, "checkpoints": True, "process": False, "virtual_desktop": False},
            "note": "directory jail + quotas only — the session is NOT hidden from the desktop by the OS",
        }

    def prepare(self, session: WorkspaceSession) -> None:
        session.note("isolation: directory jail only (no OS-level separation)")

    def release(self, session: WorkspaceSession) -> None:  # pragma: no cover - nothing to undo
        return None


class ProcessIsolation(NullIsolation):
    """The jail, plus the declared intent to run session work in its own OS process.

    ``spawn()`` is deliberately opt-in and dry-run aware: starting processes is a
    Phase 11 security decision, not something a workspace does on its own initiative.
    """

    name = "process"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.cfg = self.settings.workspace
        self.spawns: list[dict[str, Any]] = []
        #: live handles, kept so nothing is orphaned and callers can wait()/poll()
        self.processes: list[subprocess.Popen[bytes]] = []

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described["backend"] = self.name
        described["capabilities"] = {**described["capabilities"], "process": True}
        described["allow_spawn"] = bool(self.cfg.allow_spawn)
        described["spawns"] = len(self.spawns)
        described["running"] = sum(1 for item in self.processes if item.poll() is None)
        described["note"] = (
            "directory jail + separate-process intent"
            + ("" if self.cfg.allow_spawn else " (spawning is disabled: STAR_WORKSPACE_ALLOW_SPAWN=false)")
        )
        return described

    def prepare(self, session: WorkspaceSession) -> None:
        session.note("isolation: work is declared to run in a separate process")

    def spawn(self, session: WorkspaceSession, argv: list[str], *, dry_run: bool | None = None) -> dict[str, Any]:
        """Start ``argv`` inside the session's jail — only when explicitly allowed."""
        command = [str(part) for part in (argv or [])]
        if not command:
            return {"ok": False, "error": "field 'argv' is required"}
        effective_dry_run = self.settings.security.dry_run if dry_run is None else bool(dry_run)
        record = {"session_id": session.session_id, "argv": command, "dry_run": effective_dry_run, "pid": None}

        if effective_dry_run:
            record["note"] = "dry-run: recorded the intended spawn, no process was started"
            self.spawns.append(record)
            return {"ok": True, "dry_run": True, **record}

        if not self.cfg.allow_spawn:
            record["error"] = "spawning processes is disabled (STAR_WORKSPACE_ALLOW_SPAWN=false)"
            self.spawns.append(record)
            _log.warning("star2.workspace.spawn.refused session=%s argv=%s", session.session_id, command[:3])
            return {"ok": False, "blocked_by_policy": True, **record}

        try:
            process = subprocess.Popen(  # noqa: S603 — opt-in, jailed cwd, no shell
                command, cwd=str(session.root), shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 — report, never crash the manager
            record["error"] = f"could not start the process: {exc}"
            self.spawns.append(record)
            return {"ok": False, **record}

        self.processes.append(process)
        session.pid = process.pid
        record["pid"] = process.pid
        record["note"] = f"started pid {process.pid} with cwd inside the session jail"
        self.spawns.append(record)
        session.note(record["note"])
        return {"ok": True, "dry_run": False, **record}


class VirtualDesktopIsolation(ProcessIsolation):
    """The Windows-only strategy — an honest placeholder until a real API is chosen.

    Reporting *unavailable* (with a reason and a fallback) is the correct behaviour
    here: the blueprint forbids hard-coding one virtual-desktop technology into the
    agents, so the seam exists and the agents never learn which one it is.
    """

    name = "windows_virtual_desktop"

    def available(self) -> bool:
        return False

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described["backend"] = self.name
        described["available"] = False
        described["platform"] = sys.platform
        described["capabilities"] = {**described["capabilities"], "virtual_desktop": False}
        described["note"] = (
            "no virtual-desktop API is wired yet (Windows-only by design); "
            f"running on {sys.platform} — falling back to the process/directory jail"
        )
        return described

    def prepare(self, session: WorkspaceSession) -> None:
        super().prepare(session)
        session.note("isolation: windows_virtual_desktop requested but unavailable — using the directory jail")


#: fallback order when the requested backend cannot deliver
_FALLBACKS = {"windows_virtual_desktop": "process", "process": "null", "null": "null"}


def build_isolation(settings: Settings | None = None) -> tuple[IsolationBackend, str]:
    """Pick the isolation backend, falling back honestly. Returns ``(backend, note)``."""
    cfg = settings or Settings()
    requested = str(cfg.workspace.isolation_backend or "null").strip().lower()
    known = {"null": NullIsolation, "process": ProcessIsolation, "windows_virtual_desktop": VirtualDesktopIsolation}

    if requested not in known:
        backend = NullIsolation(cfg)
        note = f"unknown isolation backend {requested!r} — using 'null' (directory jail)"
        _log.warning("star2.workspace.isolation.unknown requested=%s", requested)
        return backend, note

    backend = known[requested](cfg)
    if backend.available():
        return backend, f"isolation backend '{requested}' is active"

    fallback_name = _FALLBACKS.get(requested, "null")
    fallback = known[fallback_name](cfg)
    note = f"isolation backend '{requested}' is unavailable — fell back to '{fallback_name}'"
    _log.info("star2.workspace.isolation.fallback requested=%s using=%s", requested, fallback_name)
    return fallback, note

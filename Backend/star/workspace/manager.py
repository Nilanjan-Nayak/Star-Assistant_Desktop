"""The invisible background workspace — sessions, jail, quotas, checkpoints.

Blueprint §8 puts the workspace in the flow itself:

``Voice → Plan → Permission → **Workspace** → Observe → Act → Observe → Verify → Recover/Complete``

So a background session is where the browser/computer/file agents put everything a
run produces — profiles, screenshots, scraped text, intermediate files — instead of
the user's visible project tree. The visible Star frontend only ever sees progress,
results and (optionally) an activity indicator.

What this manager guarantees, in order of importance:

1. **The jail.** Every path is resolved with ``os.path.realpath`` and must stay inside
   the session root, which must itself stay inside ``STAR_WORKSPACE_ROOT``. ``..``,
   absolute paths and symlink hops are refused, counted and emitted as
   ``workspace.blocked`` — an escape attempt is a security event, not an error to hide.
2. **Bounds.** Sessions expire (TTL), the alive-session budget is capped, and every
   session has file/byte quotas with a per-write cap. Checkpoints are capped too.
3. **Honesty.** ``describe()``/``state()`` report what really exists on disk (a fresh
   scan, not a cached guess), and every capability that is *not* available says so
   with a reason (see :mod:`.isolation`).
4. **Dry-run by default.** A session inherits ``security.dry_run``; destructive calls
   (``destroy``) and process spawns are refused in dry-run with a recorded intent.

Nothing here talks to the OS beyond the filesystem and (opt-in) ``subprocess``; the
low-level computer control stays in ``agent/`` where it already lives.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase
from Backend.star.observability.logging import star_logger
from Backend.star.workspace.isolation import IsolationBackend, ProcessIsolation, build_isolation
from Backend.star.workspace.session import (
    Checkpoint,
    WorkspaceKind,
    WorkspaceSession,
    WorkspaceState,
    new_session_id,
)

__all__ = ["WorkspaceError", "WorkspaceManager", "build_workspace"]

_log = star_logger("star2.workspace")

#: directory inside a session that holds snapshots — user writes are refused here
CHECKPOINT_DIR = "_checkpoints"
MANIFEST_NAME = "manifest.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_kind(kind: Any) -> WorkspaceKind:
    """Accept a :class:`WorkspaceKind`, its value, or free text (unknown ⇒ GENERIC).

    ``str()`` on a ``str``-mixin enum member gives ``"WorkspaceKind.BROWSER"`` in
    Python 3.11+, so enum instances must be handled before any string coercion.
    """
    if isinstance(kind, WorkspaceKind):
        return kind
    text = str(kind or "").strip().lower() or "generic"
    try:
        return WorkspaceKind(text)
    except ValueError:
        return WorkspaceKind.GENERIC


class WorkspaceError(RuntimeError):
    """A refused or impossible workspace operation. Carries a machine-readable code."""

    def __init__(self, message: str, *, code: str = "error", session_id: str = "", blocked_by_policy: bool = False) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.session_id = session_id
        self.blocked_by_policy = blocked_by_policy

    def public(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "code": self.code,
            "session_id": self.session_id,
            "blocked_by_policy": self.blocked_by_policy,
        }


class WorkspaceManager:
    """Creates, jails, checkpoints, expires and destroys background sessions."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: Any = None,
        isolation: IsolationBackend | None = None,
        audit: Any = None,
    ) -> None:
        self.settings = settings or Settings()
        self.cfg = self.settings.workspace
        self.paths = self.settings.paths
        self.bus = bus
        self.audit = audit
        self.root = Path(self.paths.workspace_root)
        self.isolation, self.isolation_note = (isolation, f"isolation backend '{isolation.name}' supplied") if isolation else build_isolation(self.settings)
        self._sessions: dict[str, WorkspaceSession] = {}
        self.stats: dict[str, int] = {
            "created": 0, "reused": 0, "closed": 0, "destroyed": 0, "expired": 0,
            "checkpoints": 0, "restores": 0, "writes": 0, "reads": 0, "refused": 0, "escapes": 0,
        }
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("workspace root %s is not writable: %s", self.root, exc)
        self.expire()
        _log.info("star2.workspace.ready root=%s isolation=%s max_sessions=%d", self.root, self.isolation.name, self.cfg.max_sessions)

    async def aclose(self) -> None:
        """Mark alive sessions closed. Files are kept — deleting on shutdown would be rude."""
        if not self._started:
            return
        self._started = False
        for session in list(self._sessions.values()):
            if session.alive:
                session.state = WorkspaceState.CLOSED
                session.note("manager closed the session at shutdown")

    # ── sessions ──────────────────────────────────────────────────────────
    def create(
        self,
        kind: str | WorkspaceKind = WorkspaceKind.GENERIC,
        label: str = "",
        *,
        session_id: str = "",
        agent: str = "",
        ttl_s: float | None = None,
        dry_run: bool | None = None,
    ) -> WorkspaceSession:
        """Create a fresh jailed session. Raises :class:`WorkspaceError` when the budget is full."""
        if not self.cfg.enabled:
            raise WorkspaceError("the background workspace is disabled (STAR_WORKSPACE_ENABLED=false)", code="disabled", blocked_by_policy=True)
        self.expire()

        alive = [item for item in self._sessions.values() if item.alive]
        if len(alive) >= self.cfg.max_sessions:
            ids = ", ".join(sorted(item.session_id for item in alive))
            raise WorkspaceError(
                f"workspace budget: {len(alive)} session(s) alive (max {self.cfg.max_sessions}) — close one first: {ids}",
                code="budget",
                blocked_by_policy=True,
            )

        wanted = _coerce_kind(kind)

        sid = str(session_id or "").strip() or new_session_id()
        if sid in self._sessions:
            raise WorkspaceError(f"session {sid} already exists", code="duplicate", session_id=sid)

        root = self.root / _safe_name(sid)
        ttl = self.cfg.session_ttl_s if ttl_s is None else max(1.0, float(ttl_s))
        session = WorkspaceSession(
            session_id=sid,
            kind=wanted,
            label=str(label or "")[:120],
            root=root,
            state=WorkspaceState.IDLE,
            isolation=self.isolation.name,
            dry_run=self.settings.security.dry_run if dry_run is None else bool(dry_run),
            created_at=_utcnow(),
            expires_at=_utcnow() + timedelta(seconds=ttl),
            agent=str(agent or "")[:40],
        )

        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / CHECKPOINT_DIR).mkdir(exist_ok=True)
        except OSError as exc:
            session.state = WorkspaceState.ERROR
            session.error = f"could not create the session directory: {exc}"
            self._sessions[sid] = session
            self._refuse("create", session, session.error, code="filesystem")
            raise WorkspaceError(session.error, code="filesystem", session_id=sid) from exc

        self.isolation.prepare(session)
        self._sessions[sid] = session
        self.stats["created"] += 1
        self._emit("workspace.created", session, path=str(root), isolation=session.isolation)
        _log.info("star2.workspace.created session=%s kind=%s root=%s dry_run=%s", sid, session.kind.value, root, session.dry_run)
        return session

    def acquire(
        self,
        kind: str | WorkspaceKind = WorkspaceKind.GENERIC,
        *,
        agent: str = "",
        label: str = "",
        session_id: str = "",
        dry_run: bool | None = None,
    ) -> WorkspaceSession:
        """Reuse a matching alive session, or create one. The entry point agents use."""
        if session_id:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.usable:
                return self._reuse(existing)
        wanted = _coerce_kind(kind)
        for candidate in sorted(
            (item for item in self._sessions.values() if item.usable and item.kind is wanted),
            key=lambda item: item.last_used_at,
            reverse=True,
        ):
            if agent and candidate.agent and candidate.agent != agent:
                continue
            return self._reuse(candidate)
        return self.create(wanted, label, agent=agent, dry_run=dry_run)

    def _reuse(self, session: WorkspaceSession) -> WorkspaceSession:
        session.touch()
        if session.state is WorkspaceState.SUSPENDED:
            session.state = WorkspaceState.ACTIVE
            session.note("resumed from a checkpoint")
        self.stats["reused"] += 1
        self._emit("workspace.used", session, reused=True)
        return session

    def get(self, session_id: str) -> WorkspaceSession | None:
        return self._sessions.get(str(session_id or ""))

    def require(self, session_id: str) -> WorkspaceSession:
        session = self.get(session_id)
        if session is None:
            raise WorkspaceError(f"unknown workspace session '{session_id}'", code="unknown_session", session_id=str(session_id))
        return session

    def find(self, kind: str | WorkspaceKind | None = None, *, agent: str = "") -> WorkspaceSession | None:
        wanted = None if kind is None else _coerce_kind(kind)
        matches = [
            item for item in self._sessions.values()
            if item.usable and (wanted is None or item.kind is wanted) and (not agent or item.agent == agent)
        ]
        return max(matches, key=lambda item: item.last_used_at) if matches else None

    def sessions(self) -> list[WorkspaceSession]:
        return sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)

    def describe(self) -> list[dict[str, Any]]:
        """The contract ``StarApplication.workspaces()`` returns (newest first)."""
        return [self._public(session) for session in self.sessions()]

    def _public(self, session: WorkspaceSession) -> dict[str, Any]:
        data = session.public()
        data.update(self.scan(session))
        return data

    def state(self) -> dict[str, Any]:
        """Manager-level summary for ``GET /api/v1/workspaces`` and the console."""
        alive = [item for item in self._sessions.values() if item.alive]
        return {
            "ok": True,
            "enabled": self.cfg.enabled,
            "root": str(self.root),
            "root_exists": self.root.is_dir(),
            "isolation": self.isolation.describe(),
            "isolation_note": self.isolation_note,
            "dry_run": self.settings.security.dry_run,
            "sessions_alive": len(alive),
            "sessions_max": self.cfg.max_sessions,
            "sessions_tracked": len(self._sessions),
            "session_ttl_s": self.cfg.session_ttl_s,
            "quotas": {
                "max_files": self.cfg.max_files,
                "max_bytes": self.cfg.max_bytes,
                "max_write_bytes": self.cfg.max_write_bytes,
                "max_checkpoints": self.cfg.max_checkpoints,
                "checkpoint_max_bytes": self.cfg.checkpoint_max_bytes,
            },
            "checkpoints_enabled": self.cfg.checkpoint_enabled,
            "allow_spawn": self.cfg.allow_spawn,
            "workspaces": self.describe(),
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if not self.cfg.enabled:
            problems.append("the background workspace is disabled")
        if not self.root.is_dir():
            problems.append(f"workspace root {self.root} does not exist")
        alive = len([item for item in self._sessions.values() if item.alive])
        if alive >= self.cfg.max_sessions:
            problems.append(f"session budget full ({alive}/{self.cfg.max_sessions})")
        if self.stats["escapes"]:
            problems.append(f"{self.stats['escapes']} path escape attempt(s) refused")
        return {"status": "degraded" if problems else "ok", "detail": {"problems": problems, **self.stats}}

    # ── the jail ──────────────────────────────────────────────────────────
    def resolve(self, session: WorkspaceSession, relative: str | Path, *, write: bool = False) -> Path:
        """Map a user-supplied path into the session, refusing every escape.

        Refusals are counted, audited and emitted — a jail escape attempt is a
        security event worth seeing.
        """
        raw = str(relative or "").strip()
        if not raw:
            raise WorkspaceError("field 'path' is required", code="empty_path", session_id=session.session_id)
        if len(raw) > 1024:
            raise WorkspaceError("path is too long (>1024 characters)", code="path_length", session_id=session.session_id)

        candidate = Path(raw)
        base = candidate if candidate.is_absolute() else session.root / candidate
        try:
            resolved = Path(os.path.realpath(base))
            root = Path(os.path.realpath(session.root))
        except OSError as exc:
            raise WorkspaceError(f"cannot resolve that path: {exc}", code="filesystem", session_id=session.session_id) from exc

        if not resolved.is_relative_to(root):
            self.stats["escapes"] += 1
            message = f"path '{raw}' escapes the session jail ({root})"
            self._refuse("resolve", session, message, code="jail_escape", blocked_by_policy=True)
            raise WorkspaceError(message, code="jail_escape", session_id=session.session_id, blocked_by_policy=True)

        workspace_root = Path(os.path.realpath(self.root))
        if not root.is_relative_to(workspace_root) or root == workspace_root:
            message = f"session root {root} is not inside the workspace root {workspace_root}"
            self._refuse("resolve", session, message, code="jail_escape", blocked_by_policy=True)
            raise WorkspaceError(message, code="jail_escape", session_id=session.session_id, blocked_by_policy=True)

        if write:
            try:
                parts = resolved.relative_to(root).parts
            except ValueError:  # pragma: no cover - guarded above
                parts = ()
            if parts and parts[0] == CHECKPOINT_DIR:
                message = f"'{CHECKPOINT_DIR}/' holds snapshots — write somewhere else in the session"
                self._refuse("resolve", session, message, code="reserved_path", blocked_by_policy=True)
                raise WorkspaceError(message, code="reserved_path", session_id=session.session_id, blocked_by_policy=True)
            if not session.usable:
                raise WorkspaceError(
                    f"session {session.session_id} is {session.state.value} — nothing can be written to it",
                    code="not_usable", session_id=session.session_id,
                )
        return resolved

    def scan(self, session: WorkspaceSession) -> dict[str, int]:
        """Recount what is really on disk (excluding the snapshot directory)."""
        files = 0
        total = 0
        root = Path(os.path.realpath(session.root)) if session.root.exists() else session.root
        if root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if path.relative_to(root).parts[:1] == (CHECKPOINT_DIR,):
                        continue
                    total += path.stat().st_size
                    files += 1
                except OSError:
                    continue
        session.files = files
        session.bytes = total
        return {"files": files, "bytes": total}

    # ── files ─────────────────────────────────────────────────────────────
    def write_text(self, session: WorkspaceSession, path: str, text: str, *, dry_run: bool | None = None) -> dict[str, Any]:
        body = str(text if text is not None else "")
        encoded = body.encode("utf-8")
        if len(encoded) > self.cfg.max_write_bytes:
            message = f"write refused: {len(encoded)} bytes exceeds the {self.cfg.max_write_bytes}-byte cap"
            self._refuse("write_text", session, message, code="write_cap", blocked_by_policy=True)
            raise WorkspaceError(message, code="write_cap", session_id=session.session_id, blocked_by_policy=True)

        target = self.resolve(session, path, write=True)
        self.scan(session)
        if session.files >= self.cfg.max_files and not target.exists():
            message = f"write refused: the session already holds {session.files} files (max {self.cfg.max_files})"
            self._refuse("write_text", session, message, code="quota_files", blocked_by_policy=True)
            raise WorkspaceError(message, code="quota_files", session_id=session.session_id, blocked_by_policy=True)
        if session.bytes + len(encoded) > self.cfg.max_bytes:
            message = f"write refused: the session is at {session.bytes} bytes (max {self.cfg.max_bytes})"
            self._refuse("write_text", session, message, code="quota_bytes", blocked_by_policy=True)
            raise WorkspaceError(message, code="quota_bytes", session_id=session.session_id, blocked_by_policy=True)

        effective_dry_run = session.dry_run if dry_run is None else bool(dry_run)
        session.touch()
        relative = str(target.relative_to(Path(os.path.realpath(session.root))))
        if effective_dry_run:
            self._emit("workspace.write", session, path=relative, bytes=len(encoded), dry_run=True)
            return {
                "ok": True, "dry_run": True, "session_id": session.session_id, "path": relative,
                "bytes": len(encoded), "note": "dry-run: the file was not written — the intent is recorded",
            }

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise WorkspaceError(f"could not write {relative}: {exc}", code="filesystem", session_id=session.session_id) from exc

        self.stats["writes"] += 1
        self.scan(session)
        session.state = WorkspaceState.ACTIVE
        self._emit("workspace.write", session, path=relative, bytes=len(encoded), dry_run=False)
        return {"ok": True, "dry_run": False, "session_id": session.session_id, "path": relative,
                "bytes": len(encoded), "files": session.files, "session_bytes": session.bytes}

    def read_text(self, session: WorkspaceSession, path: str, *, limit: int = 200_000) -> dict[str, Any]:
        target = self.resolve(session, path)
        if not target.is_file():
            raise WorkspaceError(f"no such file in the session: {path}", code="not_found", session_id=session.session_id)
        try:
            size = target.stat().st_size
            body = target.read_text(encoding="utf-8", errors="replace")[: max(0, int(limit))]
        except OSError as exc:
            raise WorkspaceError(f"could not read {path}: {exc}", code="filesystem", session_id=session.session_id) from exc
        self.stats["reads"] += 1
        session.touch()
        return {
            "ok": True, "session_id": session.session_id,
            "path": str(target.relative_to(Path(os.path.realpath(session.root)))),
            "bytes": size, "truncated": size > len(body.encode("utf-8", "replace")), "text": body,
        }

    def list_files(self, session: WorkspaceSession) -> list[dict[str, Any]]:
        root = Path(os.path.realpath(session.root))
        out: list[dict[str, Any]] = []
        if not root.is_dir():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.relative_to(root).parts[:1] == (CHECKPOINT_DIR,):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append({
                "path": str(path.relative_to(root)),
                "bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            })
        return out

    # ── checkpoints ───────────────────────────────────────────────────────
    def checkpoint(self, session_id: str, label: str = "") -> dict[str, Any]:
        """Snapshot the session's files. Bounded, manifest-verified, reversible."""
        session = self.require(session_id)
        if not self.cfg.checkpoint_enabled:
            raise WorkspaceError("checkpoints are disabled (STAR_WORKSPACE_CHECKPOINTS=false)", code="disabled",
                                 session_id=session.session_id, blocked_by_policy=True)
        if not session.usable:
            raise WorkspaceError(f"session {session_id} is {session.state.value} — nothing to checkpoint",
                                 code="not_usable", session_id=session.session_id)

        root = Path(os.path.realpath(session.root))
        store = root / CHECKPOINT_DIR
        store.mkdir(parents=True, exist_ok=True)

        # keep the snapshot budget: oldest first
        while len(session.checkpoints) >= self.cfg.max_checkpoints:
            oldest = session.checkpoints.pop(0)
            shutil.rmtree(store / oldest.checkpoint_id, ignore_errors=True)
            session.note(f"dropped the oldest checkpoint {oldest.checkpoint_id} (max {self.cfg.max_checkpoints})")

        # unique even when two checkpoints land in the same second
        checkpoint_id = f"ckpt_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        destination = store / checkpoint_id
        entries: list[dict[str, Any]] = []
        copied = 0
        truncated = False

        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = path.relative_to(root)
            if relative.parts[:1] == (CHECKPOINT_DIR,):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if copied + size > self.cfg.checkpoint_max_bytes or len(entries) >= self.cfg.max_files:
                truncated = True
                continue
            target = destination / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError:
                truncated = True
                continue
            entries.append({"path": str(relative), "bytes": size, "sha256_16": digest})
            copied += size

        manifest = {
            "checkpoint_id": checkpoint_id,
            "session_id": session.session_id,
            "label": str(label or "")[:120],
            "created_at": _utcnow().isoformat(timespec="seconds"),
            "files": len(entries),
            "bytes": copied,
            "truncated": truncated,
            "entries": entries,
        }
        try:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except OSError as exc:
            raise WorkspaceError(f"could not store the checkpoint: {exc}", code="filesystem", session_id=session.session_id) from exc

        snapshot = Checkpoint(
            checkpoint_id=checkpoint_id, label=manifest["label"], files=len(entries), bytes=copied,
            truncated=truncated, path=str(destination),
            note=f"{len(entries)} file(s), {copied} byte(s)" + (" — truncated by the checkpoint cap" if truncated else ""),
        )
        session.checkpoints.append(snapshot)
        session.state = WorkspaceState.SUSPENDED
        session.touch()
        self.stats["checkpoints"] += 1
        self._emit("workspace.checkpoint", session, checkpoint_id=checkpoint_id, files=len(entries), bytes=copied, truncated=truncated)
        _log.info("star2.workspace.checkpoint session=%s id=%s files=%d bytes=%d", session.session_id, checkpoint_id, len(entries), copied)
        return {"ok": True, "session_id": session.session_id, "checkpoint": snapshot.public(), "state": session.state.value}

    def restore(self, session_id: str, checkpoint_id: str = "") -> dict[str, Any]:
        """Copy a snapshot back into the session. Refuses when the manifest is missing."""
        session = self.require(session_id)
        if not session.usable and session.state is not WorkspaceState.SUSPENDED:
            raise WorkspaceError(f"session {session_id} is {session.state.value} — cannot restore into it",
                                 code="not_usable", session_id=session.session_id)
        wanted = str(checkpoint_id or "").strip()
        snapshot = next((item for item in reversed(session.checkpoints) if not wanted or item.checkpoint_id == wanted), None)
        if snapshot is None:
            raise WorkspaceError(
                f"unknown checkpoint '{wanted}' for session {session_id} (known: {', '.join(session.checkpoint_ids()) or 'none'})",
                code="unknown_checkpoint", session_id=session.session_id,
            )

        root = Path(os.path.realpath(session.root))
        destination = root / CHECKPOINT_DIR / snapshot.checkpoint_id
        manifest_path = destination / MANIFEST_NAME
        if not manifest_path.is_file():
            raise WorkspaceError(f"checkpoint {snapshot.checkpoint_id} has no manifest on disk", code="missing_manifest",
                                 session_id=session.session_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"checkpoint manifest is unreadable: {exc}", code="missing_manifest", session_id=session.session_id) from exc

        restored = 0
        skipped: list[str] = []
        for entry in manifest.get("entries", []):
            relative = str(entry.get("path") or "")
            source = destination / relative
            target = self.resolve(session, relative, write=True) if relative else None
            if target is None or not source.is_file():
                skipped.append(relative or "<empty>")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                restored += 1
            except OSError:
                skipped.append(relative)

        session.state = WorkspaceState.ACTIVE
        session.touch()
        session.note(f"restored checkpoint {snapshot.checkpoint_id} ({restored} file(s))")
        self.scan(session)
        self.stats["restores"] += 1
        self._emit("workspace.restored", session, checkpoint_id=snapshot.checkpoint_id, files=restored, skipped=len(skipped))
        return {
            "ok": True, "session_id": session.session_id, "checkpoint_id": snapshot.checkpoint_id,
            "restored": restored, "skipped": skipped[:10], "state": session.state.value,
            "files": session.files, "bytes": session.bytes,
        }

    # ── teardown ──────────────────────────────────────────────────────────
    def close(self, session_id: str, *, destroy: bool = False, dry_run: bool | None = None) -> dict[str, Any]:
        """Close a session; optionally delete its files (inside the jail, verified twice)."""
        session = self.require(session_id)
        effective_dry_run = session.dry_run if dry_run is None else bool(dry_run)

        if destroy and effective_dry_run:
            session.state = WorkspaceState.CLOSED
            self.stats["closed"] += 1
            self._emit("workspace.closed", session, destroy=False, dry_run=True)
            return {
                "ok": True, "dry_run": True, "session_id": session.session_id, "state": session.state.value,
                "note": "dry-run: the session was closed on paper — its files were NOT deleted",
            }

        session.state = WorkspaceState.CLOSED
        session.touch()
        self.stats["closed"] += 1
        self._emit("workspace.closed", session, destroy=False, dry_run=False)

        if not destroy:
            self.scan(session)
            return {"ok": True, "dry_run": False, "session_id": session.session_id, "state": session.state.value,
                    "files": session.files, "bytes": session.bytes, "note": "files kept for inspection"}

        removed = self._destroy(session)
        return {"ok": True, "dry_run": False, "session_id": session.session_id,
                "state": session.state.value, **removed}

    def _destroy(self, session: WorkspaceSession) -> dict[str, Any]:
        """Delete a session directory — only after proving it is inside the workspace root."""
        root = Path(os.path.realpath(session.root))
        workspace_root = Path(os.path.realpath(self.root))
        if root == workspace_root or not root.is_relative_to(workspace_root):
            message = f"refusing to delete {root}: it is not a session directory inside {workspace_root}"
            self._refuse("destroy", session, message, code="jail_escape", blocked_by_policy=True)
            raise WorkspaceError(message, code="jail_escape", session_id=session.session_id, blocked_by_policy=True)
        if root.name == CHECKPOINT_DIR or root.name.startswith("_"):
            message = f"refusing to delete the reserved directory {root}"
            self._refuse("destroy", session, message, code="reserved_path", blocked_by_policy=True)
            raise WorkspaceError(message, code="reserved_path", session_id=session.session_id, blocked_by_policy=True)

        self.scan(session)
        files, size = session.files, session.bytes
        try:
            shutil.rmtree(root, ignore_errors=False)
        except OSError as exc:
            session.state = WorkspaceState.ERROR
            session.error = f"could not delete the session directory: {exc}"
            raise WorkspaceError(session.error, code="filesystem", session_id=session.session_id) from exc

        session.state = WorkspaceState.DESTROYED
        session.checkpoints.clear()
        session.files = 0
        session.bytes = 0
        session.note(f"destroyed: removed {files} file(s), {size} byte(s)")
        self.stats["destroyed"] += 1
        self._audit("workspace_destroy", session, {"destroy": True, "files": files, "bytes": size}, decision="executed", risk="high")
        self._emit("workspace.destroyed", session, files=files, bytes=size)
        _log.info("star2.workspace.destroyed session=%s files=%d bytes=%d", session.session_id, files, size)
        return {"destroyed": True, "removed_files": files, "removed_bytes": size}

    def expire(self, *, now: datetime | None = None) -> list[str]:
        """Close sessions whose TTL passed. Called on startup, before create, and by the gateway."""
        moment = now or _utcnow()
        expired: list[str] = []
        for session in list(self._sessions.values()):
            if session.is_expired(moment):
                session.state = WorkspaceState.EXPIRED
                session.note(f"expired after {self.cfg.session_ttl_s:g}s (files kept)")
                self.stats["expired"] += 1
                expired.append(session.session_id)
                self._emit("workspace.expired", session)
        if expired:
            _log.info("star2.workspace.expired count=%d ids=%s", len(expired), ",".join(expired))
        return expired

    # ── isolation extras ──────────────────────────────────────────────────
    def spawn(self, session_id: str, argv: list[str], *, dry_run: bool | None = None) -> dict[str, Any]:
        """Start a process inside the session jail — only if the backend supports it and it is allowed."""
        session = self.require(session_id)
        if not isinstance(self.isolation, ProcessIsolation):
            return {
                "ok": False, "blocked_by_policy": True, "session_id": session.session_id,
                "error": f"isolation backend '{self.isolation.name}' cannot start processes (use STAR_ISOLATION_BACKEND=process)",
            }
        outcome = self.isolation.spawn(session, argv, dry_run=dry_run)
        self._emit("workspace.spawn", session, argv=list(argv or [])[:6], ok=bool(outcome.get("ok")),
                   dry_run=bool(outcome.get("dry_run")), pid=outcome.get("pid"))
        return outcome

    def browser_profile(self, session: WorkspaceSession) -> Path:
        """Where an isolated browser profile for this session should live."""
        profile = session.root / "profile"
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self.paths.browser_profile_root
        return profile

    # ── internals ─────────────────────────────────────────────────────────
    def _emit(self, event_kind: str, session: WorkspaceSession, **payload: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(
                event_kind, phase=EventPhase.WORKSPACE, session_id=str(session.session_id or ""),
                workspace_kind=session.kind.value, state=session.state.value, **payload,
            )
        except Exception as exc:  # noqa: BLE001 — events must never break the workspace
            _log.warning("workspace event %s failed: %s", event_kind, exc)

    def _refuse(self, action: str, session: WorkspaceSession, reason: str, *, code: str, blocked_by_policy: bool = False) -> None:
        self.stats["refused"] += 1
        session.note(f"refused {action}: {reason[:120]}")
        self._audit(f"workspace_{action}", session, {"reason": reason, "code": code},
                    decision="blocked" if blocked_by_policy else "error", risk="high" if blocked_by_policy else "medium", ok=False)
        self._emit("workspace.blocked", session, action=action, code=code, reason=reason[:200], blocked_by_policy=blocked_by_policy)
        _log.warning("star2.workspace.refused session=%s action=%s code=%s reason=%s", session.session_id, action, code, reason[:120])

    def _audit(self, tool: str, session: WorkspaceSession, arguments: dict[str, Any], *, decision: str, risk: str, ok: bool = True) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record({
                "tool": tool, "decision": decision, "risk": risk, "ok": ok,
                "dry_run": bool(session.dry_run), "session_id": str(session.session_id or ""),
                "arguments": arguments, "error": None if ok else str(arguments.get("reason") or ""),
            }, kind="workspace.decision")
        except Exception as exc:  # noqa: BLE001 — auditing must never break an action
            _log.warning("workspace audit failed for %s: %s", tool, exc)


def _safe_name(raw: str) -> str:
    """A directory name that cannot escape the workspace root."""
    cleaned = "".join(char if (char.isalnum() or char in "-_.") else "_" for char in str(raw or "").strip())
    cleaned = cleaned.strip("._") or "session"
    return cleaned[:64]


def build_workspace(settings: Settings | None = None, *, bus: Any = None, audit: Any = None, isolation: IsolationBackend | None = None) -> WorkspaceManager:
    """Composition-root factory (``Backend/star/main.py``)."""
    return WorkspaceManager(settings, bus=bus, audit=audit, isolation=isolation)

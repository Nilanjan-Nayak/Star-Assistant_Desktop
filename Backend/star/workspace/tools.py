"""Workspace tools — the background session surface as typed, policy-checked ToolSpecs.

Nine tools, all registered in the Phase 4 registry, so they inherit parameter
validation, the permission ladder, confirmation for destructive calls, the audit
trail and dry-run simulation. None of them can reach outside a session jail:
every path goes through :meth:`WorkspaceManager.resolve`, which is where escape
attempts are refused, counted, audited and published as ``workspace.blocked``.

Risk ladder (feeds ``classify_risk`` → confirmation gate):

* **low** — read-only: ``workspace_list``, ``workspace_state``, ``workspace_read``,
  ``workspace_files``, ``workspace_checkpoint`` (a snapshot only ever adds files).
* **medium** — creates or mutates inside the jail: ``workspace_create``,
  ``workspace_write``, ``workspace_restore``.
* **medium → high** — ``workspace_close`` is ``medium`` while it merely closes a session
  (files kept). Passing ``remove_files=true`` adds a key the Phase 4 risk classifier reads
  as destructive, so the ladder takes ``highest(spec, keywords)`` = **high** and the call
  needs an explicit human approval above ``STAR_CONFIRM_ABOVE_RISK``; in dry-run it only
  records the intent. The argument is deliberately *not* called ``destroy``: that bare key
  would tier routine session cleanup as ``critical`` (denied by default policy) rather than
  ``high`` (confirmed), which would make the workspace impossible to clean up.
"""

from __future__ import annotations

from typing import Any

from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger
from Backend.star.tools.spec import ToolSpec
from Backend.star.workspace.manager import WorkspaceError, WorkspaceManager
from Backend.star.workspace.session import WorkspaceKind, WorkspaceSession

__all__ = ["WorkspaceToolkit", "register_workspace_tools"]

_log = star_logger("star2.workspace.tools")


class WorkspaceToolkit:
    """Thin, honest wrapper the tool handlers share.

    One invariant worth stating: **if a handler is running, the call is live.** The
    Phase 4 executor simulates ``dry_run_safe`` tools in dry-run without reaching the
    handler, so these methods pass ``dry_run=False`` down to the manager explicitly —
    otherwise a session created under a global dry-run would keep refusing real work
    after an operator approved a single live call (and vice versa).
    """

    def __init__(self, manager: WorkspaceManager, settings: Settings | None = None) -> None:
        self.manager = manager
        self.settings = settings or manager.settings
        self.stats: dict[str, int] = {"calls": 0, "ok": 0, "refused": 0, "no_session": 0}

    # ── helpers ───────────────────────────────────────────────────────────
    def session(self, session_id: str = "", *, kind: str = "generic", agent: str = "tool") -> WorkspaceSession:
        """The session a call means: the one named, else the most recently used one."""
        if str(session_id or "").strip():
            return self.manager.require(session_id)
        found = self.manager.find()
        if found is None:
            self.stats["no_session"] += 1
            raise WorkspaceError(
                "no background workspace session is open — call workspace_create first",
                code="no_session",
            )
        return self.manager._reuse(found)  # noqa: SLF001 — same package, deliberate

    def _fail(self, exc: WorkspaceError, tool: str) -> dict[str, Any]:
        self.stats["refused"] += 1
        return {
            "success": False,
            "error": f"{tool}: {exc.message}",
            "result": {"code": exc.code, "blocked_by_policy": exc.blocked_by_policy, "session_id": exc.session_id},
        }

    def _guard(self, tool: str, action: Any) -> dict[str, Any]:
        """Run one manager call, turning refusals into honest results."""
        self.stats["calls"] += 1
        try:
            outcome = action()
        except WorkspaceError as exc:
            return self._fail(exc, tool)
        except Exception as exc:  # noqa: BLE001 — a tool must never crash the executor
            self.stats["refused"] += 1
            _log.warning("workspace tool %s failed: %s", tool, exc)
            return {"success": False, "error": f"{tool}: {type(exc).__name__}: {exc}"[:300], "result": {"code": "error"}}
        payload = dict(outcome) if isinstance(outcome, dict) else {"ok": True, "result": outcome}
        self.stats["ok"] += 1
        payload.setdefault("success", bool(payload.get("ok", True)))
        return payload

    # ── the nine operations ───────────────────────────────────────────────
    def create(self, kind: str = "generic", label: str = "", ttl_s: float | None = None) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.manager.create(kind, label, agent="tool", ttl_s=ttl_s, dry_run=False)
            return {"ok": True, "session_id": session.session_id, "result": session.public()}
        return self._guard("workspace_create", action)

    def list_sessions(self) -> dict[str, Any]:
        return self._guard("workspace_list", lambda: {
            "ok": True,
            "result": {"workspaces": self.manager.describe(), "count": len(self.manager.sessions())},
        })

    def state(self) -> dict[str, Any]:
        return self._guard("workspace_state", lambda: {"ok": True, "result": self.manager.state()})

    def write(self, path: str, text: str, session_id: str = "") -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.session(session_id, agent="tool")
            return self.manager.write_text(session, path, text, dry_run=False)
        return self._guard("workspace_write", action)

    def read(self, path: str, session_id: str = "", limit: int = 200_000) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.session(session_id)
            return self.manager.read_text(session, path, limit=limit)
        return self._guard("workspace_read", action)

    def files(self, session_id: str = "") -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.session(session_id)
            entries = self.manager.list_files(session)
            return {"ok": True, "session_id": session.session_id,
                    "result": {"files": entries, "count": len(entries), "bytes": sum(item["bytes"] for item in entries)}}
        return self._guard("workspace_files", action)

    def checkpoint(self, label: str = "", session_id: str = "") -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.session(session_id)
            return self.manager.checkpoint(session.session_id, label)
        return self._guard("workspace_checkpoint", action)

    def restore(self, checkpoint_id: str = "", session_id: str = "") -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.session(session_id)
            return self.manager.restore(session.session_id, checkpoint_id)
        return self._guard("workspace_restore", action)

    def close(self, session_id: str = "", remove_files: bool = False) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            session = self.session(session_id)
            return self.manager.close(session.session_id, destroy=bool(remove_files), dry_run=False)
        return self._guard("workspace_close", action)


_KINDS = [item.value for item in WorkspaceKind]


def register_workspace_tools(
    registry: Any,
    settings: Settings | None = None,
    *,
    bus: Any = None,
    manager: WorkspaceManager | None = None,
    toolkit: WorkspaceToolkit | None = None,
) -> WorkspaceToolkit:
    """Add the workspace tools to a :class:`StarToolRegistry`; return the toolkit."""
    from Backend.star.workspace.manager import build_workspace

    cfg = settings or Settings()
    mgr = manager if manager is not None else (toolkit.manager if toolkit is not None else None)
    if mgr is None:
        mgr = build_workspace(cfg, bus=bus)
    kit = toolkit or WorkspaceToolkit(mgr, cfg)
    kit.manager = mgr
    timeout = 30.0
    common = dict(origin="star2", module="Backend.star.workspace.tools", agent="filesystem", category="files")
    tags = ("workspace", "phase7", "isolation")

    specs = (
        ToolSpec(
            name="workspace_create",
            description="Open an isolated background session (a jailed directory) for invisible work.",
            risk="medium", parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "default": "generic", "enum": _KINDS},
                    "label": {"type": "string", "default": ""},
                    "ttl_s": {"type": "number", "default": None},
                },
                "required": [],
            },
            handler=lambda kind="generic", label="", ttl_s=None: kit.create(kind, label, ttl_s),
            timeout_s=timeout, idempotent=False, reversible=True, dry_run_safe=True,
            tags=tags, **common,
        ),
        ToolSpec(
            name="workspace_list",
            description="List background workspace sessions with their state, path, files and checkpoints.",
            risk="low", parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: kit.list_sessions(),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            tags=tags, **common,
        ),
        ToolSpec(
            name="workspace_state",
            description="Workspace manager state: isolation backend, budgets, quotas, counters.",
            risk="low", parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: kit.state(),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            tags=("workspace", "phase7", "meta"), **common,
        ),
        ToolSpec(
            name="workspace_write",
            description="Write a text file inside the session jail. Capped per write and per session quota.",
            risk="medium", parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {"type": "string"},
                    "session_id": {"type": "string", "default": ""},
                },
                "required": ["path", "text"],
            },
            handler=lambda path, text, session_id="": kit.write(path, text, session_id),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            tags=tags, **common,
        ),
        ToolSpec(
            name="workspace_read",
            description="Read a text file from the session jail (length-limited, never outside the jail).",
            risk="low", parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "session_id": {"type": "string", "default": ""},
                    "limit": {"type": "integer", "default": 200000, "minimum": 0},
                },
                "required": ["path"],
            },
            handler=lambda path, session_id="", limit=200000: kit.read(path, session_id, int(limit)),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            tags=tags, **common,
        ),
        ToolSpec(
            name="workspace_files",
            description="List the files a background session currently holds, with sizes.",
            risk="low", parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string", "default": ""}},
                "required": [],
            },
            handler=lambda session_id="": kit.files(session_id),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            tags=tags, **common,
        ),
        ToolSpec(
            name="workspace_checkpoint",
            description="Snapshot the session's files so a task can be restored later (bounded, manifest-verified).",
            risk="low", parameters={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "default": ""},
                    "session_id": {"type": "string", "default": ""},
                },
                "required": [],
            },
            handler=lambda label="", session_id="": kit.checkpoint(label, session_id),
            timeout_s=timeout, idempotent=False, reversible=True, dry_run_safe=True,
            tags=("workspace", "phase7", "checkpoint"), **common,
        ),
        ToolSpec(
            name="workspace_restore",
            description="Restore a checkpoint back into the session (refuses when the manifest is missing).",
            risk="medium", parameters={
                "type": "object",
                "properties": {
                    "checkpoint_id": {"type": "string", "default": ""},
                    "session_id": {"type": "string", "default": ""},
                },
                "required": [],
            },
            handler=lambda checkpoint_id="", session_id="": kit.restore(checkpoint_id, session_id),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            tags=("workspace", "phase7", "checkpoint"), **common,
        ),
        ToolSpec(
            name="workspace_close",
            description=(
                "Close a background session (its files are kept for inspection). Pass "
                "remove_files=true to delete them inside the jail — that raises the call to "
                "high risk, so it needs your explicit approval and refuses in dry-run."
            ),
            risk="medium", parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "default": ""},
                    "remove_files": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            handler=lambda session_id="", remove_files=False: kit.close(session_id, bool(remove_files)),
            timeout_s=timeout, idempotent=True, reversible=False, dry_run_safe=True,
            tags=("workspace", "phase7", "destructive"), **common,
        ),
    )

    for spec in specs:
        # the registry announces every registration on the bus (one rich event per tool)
        registry.register(spec, replace=True)
    _log.info("star2.workspace.tools.registered count=%d isolation=%s root=%s", len(specs), mgr.isolation.name, mgr.root)
    return kit

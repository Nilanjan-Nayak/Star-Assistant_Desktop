"""The legacy policy gate — one shim that puts every existing OS action under policy.

Blueprint §2 non-negotiable #2 ("every OS action passes through the safety/policy
layer") and #3 ("dry-run by default"), §7 Phase 4 ("wrap existing tools; do not
duplicate subsystems").

The existing product funnels *every* capability through one function:
``Backend.tools.registry.execute_tool(name, **kwargs)`` — the command router, the
LLM provider pipeline and the autonomous agent all call it. Instead of rewriting
1 200 lines of router, STAR 2.0 installs a reversible shim over that single name
in every module that imported it:

    legacy handler ──► execute_tool(...)  ══►  LegacyToolGate
                                                 │  PermissionEngine.check()
                                                 │  dry-run → SIMULATE (never touches the OS)
                                                 │  risk    → NEEDS_CONFIRMATION / DENY
                                                 │  AuditLog (before + after)
                                                 └─► ToolExecutor → real handler

The shim returns the legacy result shape (``{"success": ..., "result"|"error": ...}``)
so the existing handlers keep working unchanged; it only adds ``dry_run``,
``decision`` and ``confirmation_id`` keys that the brain reports honestly.

Disable with ``STAR_LEGACY_GATE=false`` (then legacy calls bypass policy again —
useful when comparing behaviour, never in production).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
from contextvars import ContextVar
from typing import Any, Callable

from Backend.star.observability.logging import star_logger

__all__ = ["CURRENT_CALL", "LegacyToolGate"]

_log = star_logger("gate")

#: Ids for the turn currently being processed, so audited legacy calls can be
#: correlated with the request/plan/task that caused them. ``asyncio.to_thread``
#: copies the context, so the router (running in a worker thread) sees these.
CURRENT_CALL: ContextVar[dict[str, str]] = ContextVar("star_current_call", default={})


class LegacyToolGate:
    """Callable shim with the same signature as ``execute_tool``."""

    name = "legacy-gate"

    def __init__(self, executor: Any, *, enabled: bool = True, timeout_s: float = 60.0) -> None:
        self.executor = executor
        self.enabled = enabled
        self.timeout_s = timeout_s
        self._original: Callable[..., Any] | None = None
        self._patched: list[str] = []
        self._retired = False
        self.stats = {"calls": 0, "installed": 0, "bypassed": 0, "retired_hits": 0, "loop_hops": 0}

    # ── install / uninstall ───────────────────────────────────────────────
    @property
    def installed(self) -> bool:
        return bool(self._patched)

    def install(self) -> list[str]:
        """Patch every already-imported Backend module that holds ``execute_tool``."""
        if not self.enabled or self.installed:
            return list(self._patched)
        self._retired = False
        try:
            from Backend.tools.registry import execute_tool as original
        except Exception as exc:  # noqa: BLE001 — nothing to gate if tools cannot import
            _log.warning("legacy registry unavailable, gate not installed: %s", exc)
            return []
        self._original = original
        patched: list[str] = []
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("Backend") or module is None:
                continue
            try:
                if getattr(module, "execute_tool", None) is original:
                    setattr(module, "execute_tool", self)
                    patched.append(module_name)
            except Exception:  # noqa: BLE001 — read-only module namespaces
                continue
        self._patched = patched
        self.stats["installed"] = len(patched)
        _log.info("legacy tool gate installed on %d module(s): %s", len(patched), ", ".join(sorted(patched)))
        return patched

    def uninstall(self) -> int:
        """Restore the original ``execute_tool`` everywhere it is still us.

        Modules imported *after* :meth:`install` bind whatever the registry held at
        import time — i.e. this gate — so the sweep covers every loaded ``Backend``
        module, not just the ones recorded at install time. The gate also retires
        itself, so any reference that survives still forwards to the original.
        """
        self._retired = True
        if self._original is None:
            self._patched = []
            return 0
        restored = 0
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("Backend") or module is None:
                continue
            try:
                if getattr(module, "execute_tool", None) is self:
                    setattr(module, "execute_tool", self._original)
                    restored += 1
            except Exception:  # noqa: BLE001 — read-only module namespaces
                continue
        self._patched = []
        _log.info("legacy tool gate removed from %d module(s)", restored)
        return restored

    # ── the shim ──────────────────────────────────────────────────────────
    def __call__(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.stats["calls"] += 1
        if self._retired or not self.enabled or self.executor is None:
            self.stats["retired_hits" if self._retired else "bypassed"] += 1
            return self._call_original(name, **kwargs)
        ids = dict(CURRENT_CALL.get() or {})
        try:
            result = self._run(
                lambda: self.executor.call(
                    name,
                    kwargs,
                    session_id=str(ids.get("session_id") or ""),
                    request_id=str(ids.get("request_id") or ""),
                    plan_id=str(ids.get("plan_id") or ""),
                    task_id=str(ids.get("task_id") or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 — the gate must never break a legacy call
            _log.warning("gate failed for %s: %s", name, exc)
            return {"success": False, "error": f"policy gate error: {exc!r}", "decision": "gate_error"}
        return self._legacy_shape(result)

    def _call_original(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if self._original is None:
            from Backend.tools.registry import execute_tool as original

            self._original = original
        return self._original(name, **kwargs)  # type: ignore[misc]

    def _run(self, factory: Callable[[], Any]) -> Any:
        """Run an async executor call from synchronous legacy code."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())          # worker thread: no loop yet
        # Called from the event-loop thread (e.g. a direct sync call): hop once.
        self.stats["loop_hops"] += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="star-gate") as pool:
            return pool.submit(asyncio.run, factory()).result(timeout=self.timeout_s)

    @staticmethod
    def _legacy_shape(result: Any) -> dict[str, Any]:
        """Convert a :class:`ToolResult` back into the legacy dict shape."""
        payload: dict[str, Any] = {"success": bool(getattr(result, "ok", False))}
        data = getattr(result, "data", None) or {}
        if isinstance(data, dict):
            payload.update(data)
        error = getattr(result, "error", None)
        if error:
            payload["error"] = str(error)
        decision = str(getattr(result, "decision", "") or "")
        payload["decision"] = decision
        if getattr(result, "dry_run", False):
            payload["dry_run"] = True
        confirmation_id = getattr(result, "confirmation_id", None)
        if confirmation_id:
            payload["confirmation_id"] = confirmation_id
        if decision == "simulated":
            payload.setdefault("result", {"simulated": True, "tool": getattr(result, "tool", "")})
        return payload

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "installed": self.installed,
            "retired": self._retired,
            "patched_modules": sorted(self._patched),
            **self.stats,
        }

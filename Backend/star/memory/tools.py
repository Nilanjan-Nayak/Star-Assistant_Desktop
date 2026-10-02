"""Memory tools — the agent-facing surface of the Phase 8 layered memory.

Seven tools, registered in the Phase 4 registry, so they inherit parameter
validation, the permission ladder, the audit trail, confirmation for the
irreversible one, and dry-run simulation.

Deliberately **not** duplicated from what already exists: the legacy registry
already exposes ``remember_fact`` (semantic write) and ``recall_memory``
(semantic read), and the app records episodes by itself after every browser /
computer run. These tools only add what nothing else offered:

* one *blended* recall across all five layers (``memory_recall``);
* read/write access to **preferences** (``memory_list_preferences``, ``memory_preference_set``);
* visibility into **episodic** history, the **working** buffer and the whole
  memory system (``memory_list_episodes``, ``memory_read_working``, ``memory_status``);
* an auditable, confirmed way to delete one record (``memory_forget``).

Risk ladder (``highest(spec.risk, classify_risk(name, args))`` decides the tier):

* **low** — the five reads. Their names are chosen so the keyword classifier
  agrees (``recall``/``list_``/``read_``/``status``) instead of falling back to
  ``medium`` for a call that changes nothing.
* **medium** — ``memory_preference_set`` (``set_`` ⇒ a mutation, but of Star's own
  notes, never of the OS; overwriting the same key is the normal way to change it).
* **high** — ``memory_forget`` deletes a stored memory and cannot be undone, so it
  lands above ``STAR_CONFIRM_ABOVE_RISK`` and needs an explicit human yes. In
  dry-run it is simulated, like every other tool.

Handlers are **sync**: the Phase 4 executor runs them with ``asyncio.to_thread``,
and :class:`~Backend.star.memory.manager.MemoryManager` provides blocking twins
(``search``, ``*_now``) that are safe from a worker thread with no event loop.
"""

from __future__ import annotations

from typing import Any

from Backend.star.brain.schemas import AgentName
from Backend.star.config.settings import Settings
from Backend.star.memory.manager import MemoryManager
from Backend.star.observability.logging import star_logger
from Backend.star.tools.spec import ToolCategory, ToolSpec

__all__ = ["MemoryToolkit", "register_memory_tools"]

_log = star_logger("star2.memory.tools")

_TAGS: tuple[str, ...] = ("memory", "phase8", "layered")
_TIMEOUT = 20.0


class MemoryToolkit:
    """Thin, honest wrapper the tool handlers share.

    Every method returns a plain dict with ``ok``, a one-line ``output`` the brain can
    say out loud, and structured data. Nothing here raises: a dead store becomes
    ``ok=False`` with the reason, because a memory problem must never take down a reply.
    """

    def __init__(self, manager: MemoryManager | None, settings: Settings | None = None) -> None:
        self.manager = manager
        self.settings = settings or (manager.settings if manager is not None else Settings())
        self.stats: dict[str, int] = {"calls": 0, "ok": 0, "refused": 0, "no_manager": 0, "empty": 0}

    # ── helpers ───────────────────────────────────────────────────────────
    def _missing(self, tool: str) -> dict[str, Any]:
        self.stats["calls"] += 1
        self.stats["no_manager"] += 1
        return {
            "ok": False,
            "tool": tool,
            "error": "the layered memory manager is not wired in this build",
            "output": "Memory is not available right now.",
        }

    def _done(self, tool: str, payload: dict[str, Any], output: str) -> dict[str, Any]:
        self.stats["calls"] += 1
        ok = bool(payload.get("ok", True))
        self.stats["ok" if ok else "refused"] += 1
        return {"ok": ok, "tool": tool, "output": output, **payload}

    @staticmethod
    def _bound(limit: Any, default: int, ceiling: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, ceiling))

    # ── reads ─────────────────────────────────────────────────────────────
    def recall(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Blended retrieval: working + episodic + semantic + preferences + patterns."""
        if self.manager is None:
            return self._missing("memory_recall")
        text = str(query or "").strip()
        if not text:
            self.stats["calls"] += 1
            self.stats["refused"] += 1
            return {"ok": False, "tool": "memory_recall", "error": "a query is required",
                    "output": "Tell me what to remember — the search was empty."}
        top = self._bound(limit, self.settings.memory.retrieval_limit, 40)
        hits, trace = self.manager.search(text, limit=top)
        layers = sorted({hit.layer for hit in hits})
        payload = {
            "query": text[:200],
            "count": len(hits),
            "hits": [hit.model_dump() for hit in hits],
            "layers_used": layers,
            "candidates": trace.candidates,
            "duplicates_dropped": trace.get("duplicates", 0),
            "layer_timeouts": list(trace.get("timeouts") or []),
            "layer_errors": list(trace.get("errors") or []),
            "weights": self.manager.weights,
            "note": "read-only: retrieval never writes and never bypasses the permission ladder",
        }
        if not hits:
            self.stats["empty"] += 1
            return self._done(
                "memory_recall",
                payload,
                f"Nothing in memory matched “{text[:60]}” (searched {len(trace.get('per_layer') or {})} layers).",
            )
        best = hits[0]
        return self._done(
            "memory_recall",
            payload,
            f"{len(hits)} memory hit(s) across {', '.join(layers)} — best ({best.layer}, {best.score:.2f}): "
            f"{best.text[:120]}",
        )

    def list_preferences(self, limit: int = 20) -> dict[str, Any]:
        if self.manager is None:
            return self._missing("memory_list_preferences")
        top = self._bound(limit, 20, 100)
        items = self.manager.preferences(limit=top)
        if not items:
            self.stats["empty"] += 1
        return self._done(
            "memory_list_preferences",
            {
                "count": len(items),
                "preferences": items,
                "store": "MemoryStore rows of kind 'preference' (latest value per key)",
            },
            f"{len(items)} stored preference(s)." if items else "No preferences stored yet.",
        )

    def list_episodes(self, limit: int = 5) -> dict[str, Any]:
        if self.manager is None:
            return self._missing("memory_list_episodes")
        top = self._bound(limit, self.settings.memory.episode_recall, 25)
        items = self.manager.episodes(limit=top)
        if not items:
            self.stats["empty"] += 1
        succeeded = sum(1 for item in items if item.get("succeeded"))
        return self._done(
            "memory_list_episodes",
            {
                "count": len(items),
                "episodes": items,
                "succeeded": succeeded,
                "failed": len(items) - succeeded,
                "note": "only succeeded episodes are used as recall candidates by the episodic layer",
            },
            f"{len(items)} recent interaction(s): {succeeded} succeeded, {len(items) - succeeded} failed."
            if items
            else "No interactions recorded yet.",
        )

    def read_working(self, session_id: str = "", limit: int = 8) -> dict[str, Any]:
        if self.manager is None:
            return self._missing("memory_read_working")
        top = self._bound(limit, self.settings.memory.working_turns, 64)
        sid = str(session_id or "").strip() or self.manager._session()  # noqa: SLF001 — same package
        turns = self.manager.working_turns(sid)[-top:]
        if not turns:
            self.stats["empty"] += 1
        return self._done(
            "memory_read_working",
            {
                "session_id": sid,
                "count": len(turns),
                "turns": turns,
                "delegated_to_brain": self.manager.working.delegated,
                "capacity": {"turns": self.settings.memory.working_turns,
                             "sessions": self.settings.memory.working_sessions},
            },
            f"{len(turns)} turn(s) in working memory for session {sid[:12]}."
            if turns
            else "Working memory is empty for this session.",
        )

    def status(self) -> dict[str, Any]:
        if self.manager is None:
            return self._missing("memory_status")
        described = self.manager.describe()
        health = self.manager.health()
        return self._done(
            "memory_status",
            {
                "status": health.get("status"),
                "health": health.get("detail"),
                "layers": described.get("layers"),
                "limits": described.get("limits"),
                "stats": described.get("stats"),
                "last_retrieval": described.get("last_retrieval"),
                "long_term_store": described.get("long_term_store"),
                "no_second_store": True,
            },
            f"memory {health.get('status')}: "
            + ", ".join(f"{name}={info.get('weight')}" for name, info in (described.get("layers") or {}).items()),
        )

    # ── writes ────────────────────────────────────────────────────────────
    def set_preference(self, key: str, value: str) -> dict[str, Any]:
        if self.manager is None:
            return self._missing("memory_preference_set")
        pref_key = str(key or "").strip()
        pref_value = str(value or "").strip()
        if not pref_key or not pref_value:
            self.stats["calls"] += 1
            self.stats["refused"] += 1
            return {"ok": False, "tool": "memory_preference_set",
                    "error": "both 'key' and 'value' are required",
                    "output": "I need both a key and a value to store a preference."}
        result = self.manager.set_preference_now(pref_key, pref_value)
        if not result.get("ok"):
            return self._done("memory_preference_set", result, f"Could not store the preference: {result.get('error')}")
        return self._done(
            "memory_preference_set",
            result,
            f"Remembered your preference: {result.get('key')} = {result.get('value')}.",
        )

    def forget(self, memory_id: Any) -> dict[str, Any]:
        if self.manager is None:
            return self._missing("memory_forget")
        result = self.manager.forget_now(memory_id)
        if not result.get("ok"):
            return self._done(
                "memory_forget",
                result,
                f"Nothing was deleted ({result.get('error') or 'no such memory'}).",
            )
        return self._done(
            "memory_forget",
            result,
            f"Deleted memory #{result.get('memory_id')} from the long-term store.",
        )


def register_memory_tools(
    registry: Any,
    settings: Settings | None = None,
    *,
    bus: Any = None,
    manager: MemoryManager | None = None,
    toolkit: MemoryToolkit | None = None,
) -> MemoryToolkit:
    """Add the seven memory tools to a :class:`StarToolRegistry`; return the toolkit.

    ``manager=None`` is allowed (the tools then answer honestly that memory is not
    wired) so the registry surface stays complete in every build.
    """
    cfg = settings or (manager.settings if manager is not None else Settings())
    kit = toolkit or MemoryToolkit(manager, cfg)
    if manager is not None:
        kit.manager = manager
    common: dict[str, Any] = {
        "origin": "star2",
        "module": "Backend.star.memory.tools",
        "agent": AgentName.CONVERSATION,
        "category": ToolCategory.MEMORY,
    }
    query_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what to look for in memory"},
            "limit": {"type": "integer", "default": cfg.memory.retrieval_limit, "description": "max hits"},
        },
        "required": ["query"],
    }

    specs = (
        ToolSpec(
            name="memory_recall",
            description=(
                "Search every memory layer at once (working, episodic, semantic, preferences, procedural "
                "patterns) and return one blended, weighted ranking. Read-only."
            ),
            risk="low",
            parameters=query_schema,
            handler=lambda query, limit=cfg.memory.retrieval_limit: kit.recall(query, limit),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="memory_list_preferences",
            description="List the user's stored preferences (latest value per key) from long-term memory.",
            risk="low",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
                "required": [],
            },
            handler=lambda limit=20: kit.list_preferences(limit),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="memory_list_episodes",
            description="List recent recorded interactions (goal, steps, succeeded) from episodic memory.",
            risk="low",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": cfg.memory.episode_recall}},
                "required": [],
            },
            handler=lambda limit=cfg.memory.episode_recall: kit.list_episodes(limit),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="memory_read_working",
            description="Read the current session's working memory (the last few turns Star is holding).",
            risk="low",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "default": "", "description": "empty = the active session"},
                    "limit": {"type": "integer", "default": cfg.memory.working_turns},
                },
                "required": [],
            },
            handler=lambda session_id="", limit=cfg.memory.working_turns: kit.read_working(session_id, limit),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="memory_status",
            description=(
                "Report the memory system: which layers are available, what store backs each one, the "
                "layer weights, limits, counters and the last retrieval trace."
            ),
            risk="low",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: kit.status(),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=(*_TAGS, "meta"), **common,
        ),
        ToolSpec(
            name="memory_preference_set",
            description=(
                "Store a user-approved preference (key/value) in long-term memory. Setting the same key "
                "again updates it; the previous value stays in the audit trail."
            ),
            risk="medium",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "short stable key, e.g. 'reply_language'"},
                    "value": {"type": "string", "description": "the value to remember"},
                },
                "required": ["key", "value"],
            },
            handler=lambda key, value: kit.set_preference(key, value),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="memory_forget",
            description=(
                "Delete one long-term memory record by id. Irreversible, so it is high-risk and needs an "
                "explicit human confirmation (and is only simulated while dry-run is on)."
            ),
            risk="high",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "id returned by memory_recall / memory_status"},
                },
                "required": ["memory_id"],
            },
            handler=lambda memory_id: kit.forget(memory_id),
            timeout_s=_TIMEOUT, idempotent=False, reversible=False, dry_run_safe=True,
            tags=(*_TAGS, "destructive"), **common,
        ),
    )

    for spec in specs:
        # the registry announces every registration on the bus (one rich event per tool)
        registry.register(spec, replace=True)
    _log.info("star2.memory.tools.registered count=%d manager=%s", len(specs), manager is not None)
    return kit

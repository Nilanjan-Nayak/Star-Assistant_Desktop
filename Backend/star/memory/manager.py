"""Phase 8 — the layered memory manager: one honest face over five stores.

Blueprint §9 asks for five layers (working / episodic / semantic / preferences /
procedural patterns) **and** says: *"Inspect existing implementation before
introducing another memory store."* Both are honoured here — this module owns no
storage at all:

======================  ==================================================================
layer                   existing implementation it adapts
======================  ==================================================================
working                 ``brain.context.ContextBuilder`` scratch (attached after the brain
                        is built, so there is exactly one copy of the current conversation)
episodic                ``agent.planning.memory.EpisodicMemory`` (JSONL episode log)
semantic                ``agent.memory.store.MemoryStore`` rows of kind ``fact`` (SQLite)
preference              the same store, rows of kind ``preference`` (latest value per key)
pattern (procedural)    ``brain.prediction.PatternStore`` (JSONL tool→tool counters) — **read-only**
                        in Phase 8; Phase 9 is what validates candidate patterns
======================  ==================================================================

Who talks to this object:

* the **brain** — it satisfies :class:`~Backend.star.brain.context.MemoryRetriever`
  (``name`` / ``available()`` / ``retrieve()`` / ``digest()``), so ``build_brain(...,
  retriever=manager)`` drops it in and memory is still retrieved *before* planning;
* the **gateway/console** — ``GET /api/v1/memory`` renders ``snapshot()`` (layer counts,
  stores, preferences, ranked hits); ``POST /api/v1/memory`` calls ``remember()``;
* **tools** — :mod:`Backend.star.memory.tools` exposes recall/preference/episode/state/forget;
* **observability** — every write publishes ``memory.updated``, every retrieval publishes
  ``memory.retrieved`` with its trace, failures publish ``memory.failed``. Memory is never
  invisible: what Star remembers, and why it remembered it, is on the event bus.

Threading: SQLite and JSONL reads are blocking, so async callers go through
``asyncio.to_thread`` with :attr:`MemorySettings.timeout_s`, and sync callers (the gateway
snapshot, tool handlers — the Phase 4 executor runs handlers in a worker thread) go through
``_run_sync``, which uses a private executor when an event loop is already running and
``asyncio.run`` when it is not. Nothing here can hang a request: a dead layer is reported in
the trace and retrieval continues with the layers that answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Final, Iterable, Sequence

from Backend.star.brain.schemas import MemoryHit
from Backend.star.config.settings import Settings
from Backend.star.memory.layers import (
    EpisodicLayer,
    PreferenceLayer,
    ProceduralLayer,
    SemanticLayer,
    WorkingMemory,
)
from Backend.star.memory.retrieval import RetrievalTrace, hybrid_retrieve
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

__all__ = ["MemoryManager", "build_memory", "layer_of_kind"]

_log = star_logger("star2.memory")

#: ``kind`` strings accepted by :meth:`MemoryManager.remember` → the layer that stores them.
_KIND_ALIASES: Final[dict[str, str]] = {
    "fact": "semantic",
    "facts": "semantic",
    "semantic": "semantic",
    "knowledge": "semantic",
    "info": "semantic",
    "preference": "preference",
    "preferences": "preference",
    "pref": "preference",
    "habit": "preference",
    "episode": "episodic",
    "episodic": "episodic",
    "interaction": "episodic",
    "run": "episodic",
    "working": "working",
    "turn": "working",
    "scratch": "working",
    "session": "working",
    "pattern": "pattern",
    "procedural": "pattern",
}

def layer_of_kind(kind: str | None) -> str:
    """Map a user/tool-supplied ``kind`` onto one of the five layers.

    Unknown spellings fall back to ``semantic``: a fact the user asked Star to keep is
    better stored as long-term knowledge than dropped because the label was unfamiliar.
    """
    return _KIND_ALIASES.get(str(kind or "fact").strip().lower(), "semantic")


def _derive_key(text: str) -> str:
    """A preference always needs a key; derive a stable one from the sentence."""
    tokens = [chunk for chunk in "".join(c if c.isalnum() else " " for c in str(text or "")).split() if chunk]
    key = "_".join(tokens[:4]).lower()
    return key[:40] or "preference"


class MemoryManager:
    """Retrieval + writes across the five layers, with honest failure reporting.

    Design rules kept from the blueprint:

    * **no second store** — every layer wraps something that already existed;
    * **read-only retrieval** — ``retrieve``/``search``/``snapshot`` never write;
    * **prediction ≠ permission** — the pattern layer only reports what co-occurred;
      nothing here can bypass the Phase 4 permission ladder or the safety governor;
    * **never break a reply** — a failing layer degrades to "no hits" plus a trace entry.
    """

    #: satisfies the brain's ``MemoryRetriever`` protocol
    name = "layered"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: StarEventBus | None = None,
        store: Any = None,
        episodes: Any = None,
        patterns: Any = None,
        own_store: bool | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.bus = bus or StarEventBus()
        self._store = store
        #: whoever opened the store closes it (``None`` here ⇒ we open it in ``startup``)
        self._owns_store = bool(own_store) if own_store is not None else store is None

        #: a disabled memory manager touches no disk at all — that is what the flag means
        enabled = self.settings.memory.enabled
        self.episodes_store = episodes if episodes is not None else (self._open_episodes() if enabled else None)
        self.pattern_store = patterns if patterns is not None else (self._open_patterns() if enabled else None)

        cfg = self.settings.memory
        self.working = WorkingMemory(self.settings)
        self.episodic = EpisodicLayer(self.episodes_store, recall=cfg.episode_recall)
        self.semantic = SemanticLayer(self._store, recent_scan=cfg.recent_scan)
        self.prefs = PreferenceLayer(self._store, recent_scan=cfg.recent_scan)
        self.procedural = ProceduralLayer(self.pattern_store, top=cfg.per_layer_limit)
        self.layers: dict[str, Any] = {
            "working": self.working,
            "episodic": self.episodic,
            "semantic": self.semantic,
            "preference": self.prefs,
            "pattern": self.procedural,
        }

        self.stats: dict[str, int] = {
            "retrievals": 0,
            "hits": 0,
            "writes": 0,
            "facts": 0,
            "preferences": 0,
            "episodes": 0,
            "forgets": 0,
            "failures": 0,
        }
        self.last_trace: RetrievalTrace = RetrievalTrace(returned=0)
        self._last_query: str = ""
        self._last_hits: list[MemoryHit] = []
        self._started = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="star-memory")

    # ── construction helpers ────────────────────────────────────────────────
    def _open_episodes(self) -> Any:
        """Reuse ``agent.planning.memory.EpisodicMemory`` — never a second episode log."""
        try:
            from agent.planning.memory import EpisodicMemory

            path = self.settings.paths.episodes_path
            path.parent.mkdir(parents=True, exist_ok=True)
            return EpisodicMemory(path=path, max_recall=self.settings.memory.episode_recall)
        except Exception as exc:  # noqa: BLE001 — memory must degrade, not crash the app
            _log.warning("episodic store unavailable (%s) — episodic layer stays empty", exc)
            return None

    def _open_patterns(self) -> Any:
        """Reuse the brain's ``PatternStore`` (Phase 3 already writes it via learn_bridge)."""
        try:
            from Backend.star.brain.prediction import PatternStore

            path = self.settings.paths.patterns_path
            path.parent.mkdir(parents=True, exist_ok=True)
            return PatternStore(path)
        except Exception as exc:  # noqa: BLE001
            _log.warning("pattern store unavailable (%s) — procedural layer stays empty", exc)
            return None

    # ── late attachment: share what the brain already built ─────────────────
    def attach_working(self, provider: Any) -> bool:
        """Hand working memory to the brain's :class:`ContextBuilder` (one scratch buffer)."""
        attached = self.working.attach(provider)
        if attached:
            self._emit("memory.updated", layer="working", action="attached", provider=type(provider).__name__)
        return attached

    def attach_patterns(self, patterns: Any) -> bool:
        """Use the brain's live ``PatternStore`` instead of a second reader of the same file."""
        if patterns is None:
            return False
        self.pattern_store = patterns
        self.procedural = ProceduralLayer(patterns, top=self.settings.memory.per_layer_limit)
        self.layers["pattern"] = self.procedural
        return True

    def attach_store(self, store: Any) -> bool:
        """Bind (or rebind) the long-term store; ``None`` leaves the layers unavailable."""
        self._store = store
        self.semantic = SemanticLayer(store, recent_scan=self.settings.memory.recent_scan)
        self.prefs = PreferenceLayer(store, recent_scan=self.settings.memory.recent_scan)
        self.episodic = EpisodicLayer(self.episodes_store, recall=self.settings.memory.episode_recall)
        self.layers.update({"semantic": self.semantic, "preference": self.prefs, "episodic": self.episodic})
        return store is not None

    @property
    def store(self) -> Any:
        """The shared ``MemoryStore`` (or ``None``) — the brain reuses it, it does not reopen it."""
        return self._store

    @property
    def weights(self) -> dict[str, float]:
        cfg = self.settings.memory
        return {
            "working": float(cfg.weight_working),
            "preference": float(cfg.weight_preference),
            "episodic": float(cfg.weight_episodic),
            "semantic": float(cfg.weight_semantic),
            "pattern": float(cfg.weight_pattern),
        }

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def startup(self) -> None:
        """Open the long-term store if nobody handed one over. Idempotent."""
        if self._started or self._closed:
            return
        self._started = True
        if self._store is None and self.settings.memory.enabled:
            from Backend.star.brain.pipeline import open_memory_store

            self._owns_store = True
            self.attach_store(await asyncio.to_thread(open_memory_store, self.settings))
        self._emit(
            "memory.updated",
            layer="all",
            action="startup",
            store_available=self._store is not None,
            episodic=self.episodic.available(),
            patterns=self.procedural.available(),
            enabled=self.settings.memory.enabled,
        )
        _log.info(
            "star2.memory.ready layers=%s store=%s episodes=%s patterns=%s",
            ",".join(self.layers),
            self._store is not None,
            self.episodic.available(),
            self.procedural.available(),
        )

    async def aclose(self) -> None:
        """Close what this manager opened, and stop its executor. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._started = False
        if self._store is not None and self._owns_store:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._store.close)
        self._executor.shutdown(wait=False)

    def close(self) -> None:
        """Sync twin of :meth:`aclose` (tests, CLI, interpreter shutdown)."""
        if self._closed:
            return
        self._closed = True
        if self._store is not None and self._owns_store:
            with contextlib.suppress(Exception):
                self._store.close()
        self._executor.shutdown(wait=False)

    def available(self) -> bool:
        """``True`` when at least one layer can answer (the brain uses this to decide)."""
        if not self.settings.memory.enabled or self._closed:
            return False
        return bool(
            self._store is not None
            or self.episodes_store is not None
            or self.pattern_store is not None
            or self.working.delegated
            or self.working.count() > 0
        )

    # ── sessions ────────────────────────────────────────────────────────────
    def _session(self, session_id: str = "") -> str:
        """Explicit session wins, then the one that last spoke, then the configured default."""
        return str(session_id or "").strip() or self.working.last_session or self.settings.session_id

    def remember_turn(self, session_id: str, text: str) -> None:
        """Feed working memory (sync — the gateway/app call it on every turn)."""
        if not self.settings.memory.enabled:
            return
        self.working.remember_turn(self._session(session_id), text)

    def forget_session(self, session_id: str = "") -> dict[str, Any]:
        sid = self._session(session_id)
        removed = self.working.forget(sid)
        self._emit("memory.updated", layer="working", action="forget_session", removed=removed, session_id=sid)
        return {"ok": True, "layer": "working", "session_id": sid, "removed": removed}

    def working_turns(self, session_id: str = "") -> list[str]:
        return self.working.turns_for(self._session(session_id))

    # ── retrieval (async: the brain) ────────────────────────────────────────
    async def retrieve(self, query: str, *, limit: int | None = None, session_id: str = "") -> list[MemoryHit]:
        """Ranked hits from every layer. Read-only; never raises."""
        started = time.perf_counter()
        hits, trace = await self._retrieve(query, limit=limit, session_id=session_id)
        self._publish_retrieval(query, hits, trace, started)
        return hits

    async def _retrieve(
        self, query: str, *, limit: int | None = None, session_id: str = ""
    ) -> tuple[list[MemoryHit], RetrievalTrace]:
        cfg = self.settings.memory
        text = str(query or "").strip()
        trace = RetrievalTrace(
            candidates=0, per_layer={}, duplicates=0, timeouts=[], errors=[], query=text[:160], returned=0
        )
        if not cfg.enabled:
            trace["error"] = "memory disabled (STAR_MEMORY_ENABLED=false)"
            return [], trace
        if not text:
            return [], trace
        try:
            hits, trace = await hybrid_retrieve(
                self.layers,
                text,
                limit=max(1, min(int(limit or cfg.retrieval_limit), 40)),
                session_id=self._session(session_id),
                weights=self.weights,
                per_layer=cfg.per_layer_limit,
                timeout_s=cfg.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — retrieval must never break a reply
            self.stats["failures"] += 1
            trace["errors"].append(f"hybrid: {type(exc).__name__}: {exc}"[:160])
            _log.warning("hybrid retrieval failed: %s", exc)
            return [], trace
        self._last_query, self._last_hits = text, list(hits)
        return hits, trace

    async def digest(self, query: str, *, limit: int = 6) -> str:
        """A short, layered context block for the prompt (``Context.memory_block``).

        Reuses the hits from the ``retrieve()`` the context builder just made — the brain
        calls both for the same query, and paying twice for the same answer would be silly.
        """
        text = str(query or "").strip()
        if not text or not self.settings.memory.enabled:
            return ""
        lines: list[str] = []
        if self._store is not None:
            try:
                block = await asyncio.wait_for(
                    asyncio.to_thread(self._store.build_context_block, text, top_k=max(1, limit)),
                    timeout=self.settings.memory.timeout_s,
                )
                lines.extend(str(block or "").splitlines())
            except Exception as exc:  # noqa: BLE001
                _log.warning("memory digest failed: %s", exc)
        hits = self._last_hits if self._last_query == text else []
        already = "\n".join(lines).lower()
        for hit in hits[: max(1, limit)]:
            if hit.layer == "semantic":
                continue                      # already inside the store's own block
            if hit.text.lower() in already:
                continue                      # preferences live in the store block too
            lines.append(f"- ({hit.layer}) {hit.text}")
        if not lines:
            return ""
        if not lines[0].lower().startswith("relevant"):
            lines.insert(0, "What Star remembers:")
        return "\n".join(lines[: max(1, limit) + 6])

    # ── retrieval (sync: gateway snapshot + tool handlers) ──────────────────
    def search(
        self, query: str, *, limit: int | None = None, session_id: str = ""
    ) -> tuple[list[MemoryHit], RetrievalTrace]:
        """Blocking :meth:`retrieve` — safe from a sync handler or a running event loop."""
        started = time.perf_counter()
        hits, trace = self._run_sync(
            lambda: self._retrieve(query, limit=limit, session_id=session_id),
            fallback=([], RetrievalTrace(candidates=0, per_layer={}, duplicates=0, timeouts=[], errors=[], returned=0)),
        )
        self._publish_retrieval(query, hits, trace, started)
        return hits, trace

    def _run_sync(self, factory: Callable[[], Any], *, fallback: Any = None, extra_timeout: float = 2.0) -> Any:
        """Run a coroutine from sync code without ever blocking forever."""
        timeout = float(self.settings.memory.timeout_s) + extra_timeout
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())          # worker thread / CLI: no loop to disturb
        future = self._executor.submit(asyncio.run, factory())
        try:
            return future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — includes TimeoutError
            self.stats["failures"] += 1
            _log.warning("sync memory call failed: %s", exc)
            return fallback

    # ── writes (async) ──────────────────────────────────────────────────────
    async def remember(
        self,
        text: str,
        *,
        kind: str = "fact",
        key: str | None = None,
        value: str | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Store one thing in the right layer. ``kind`` decides the layer (see module docs)."""
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error": "field 'text' is required"}
        if not self.settings.memory.enabled:
            return {"ok": False, "layer": None, "error": "memory is disabled (STAR_MEMORY_ENABLED=false)"}
        layer = layer_of_kind(kind)
        sid = self._session(session_id)
        started = time.perf_counter()
        try:
            if layer == "working":
                self.working.remember_turn(sid, body)
                self.stats["writes"] += 1
                result: dict[str, Any] = {
                    "ok": True,
                    "layer": "working",
                    "session_id": sid,
                    "turns": self.working.count(sid),
                    "text": body[:200],
                }
            elif layer == "episodic":
                result = await self.record_episode(body, session_id=sid)
            elif layer == "preference":
                result = await self.set_preference(
                    key or _derive_key(body), value or body, text=body, session_id=sid, emit=False
                )
            elif layer == "pattern":
                result = {
                    "ok": False,
                    "layer": "pattern",
                    "error": "procedural patterns are learned from finished runs, not written by hand "
                    "(Phase 9 validates candidates)",
                }
            else:
                record = await asyncio.wait_for(
                    asyncio.to_thread(self.semantic.remember, body, key=key, value=value),
                    timeout=self.settings.memory.timeout_s,
                )
                self.stats["facts"] += 1
                self.stats["writes"] += 1
                result = {
                    "ok": True,
                    "layer": "semantic",
                    "memory_kind": "fact",
                    "memory_id": int(getattr(record, "memory_id", 0) or 0),
                    "text": str(getattr(record, "text", body))[:200],
                    "key": getattr(record, "key", None),
                    "value": getattr(record, "value", None),
                }
        except Exception as exc:  # noqa: BLE001 — a failed write is reported, never raised
            self.stats["failures"] += 1
            self._emit(
                "memory.failed",
                layer=layer,
                action="remember",
                session_id=sid,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            return {"ok": False, "layer": layer, "error": f"{type(exc).__name__}: {exc}"[:300]}

        # ``set_preference`` / ``record_episode`` already counted their own write
        self._emit(
            "memory.updated" if result.get("ok") else "memory.failed",
            layer=str(result.get("layer") or layer),
            action="remember",
            session_id=sid,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            memory_kind=str(result.get("memory_kind") or layer),
            memory_id=result.get("memory_id"),
            key=result.get("key"),
            value=result.get("value"),
            episode_id=result.get("episode_id"),
            text=str(result.get("text") or body)[:160],
            **({} if result.get("ok") else {"error": str(result.get("error"))[:200]}),
        )
        return result

    async def set_preference(
        self,
        key: str,
        value: str,
        *,
        text: str = "",
        session_id: str = "",
        emit: bool = True,
    ) -> dict[str, Any]:
        """Remember a user-approved habit (``MemoryKind.PREFERENCE``, latest value per key)."""
        if not self.settings.memory.enabled:
            return {"ok": False, "layer": "preference", "error": "memory is disabled"}
        pref_key = str(key or "").strip()[:80]
        pref_value = str(value if value is not None else "").strip()[:400]
        if not pref_key:
            return {"ok": False, "layer": "preference", "error": "a preference needs a key"}
        if not pref_value:
            return {"ok": False, "layer": "preference", "error": "a preference needs a value"}
        sid = self._session(session_id)
        try:
            record = await asyncio.wait_for(
                asyncio.to_thread(self.prefs.set, pref_key, pref_value, text=text or f"{pref_key} = {pref_value}"),
                timeout=self.settings.memory.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            self.stats["failures"] += 1
            self._emit(
                "memory.failed", layer="preference", action="preference_set", session_id=sid, error=repr(exc)[:200]
            )
            return {"ok": False, "layer": "preference", "error": f"{type(exc).__name__}: {exc}"[:300]}
        self.stats["preferences"] += 1
        self.stats["writes"] += 1
        result = {
            "ok": True,
            "layer": "preference",
            "memory_kind": "preference",
            "key": pref_key,
            "value": pref_value,
            "memory_id": int(getattr(record, "memory_id", 0) or 0),
            "session_id": sid,
        }
        if emit:
            self._emit("memory.updated", layer="preference", action="preference_set", session_id=sid, **{
                "key": pref_key, "value": pref_value, "memory_id": result["memory_id"],
            })
        return result

    async def record_episode(
        self,
        goal: str,
        *,
        steps: Iterable[dict[str, Any]] | Sequence[dict[str, Any]] | None = (),
        succeeded: bool = True,
        session_id: str = "",
        agent: str = "",
        started_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one finished interaction (what was attempted, what happened)."""
        if not self.settings.memory.enabled:
            return {"ok": False, "layer": "episodic", "error": "memory is disabled"}
        text = str(goal or "").strip()
        if not text:
            return {"ok": False, "layer": "episodic", "error": "an episode needs a goal"}
        if self.episodes_store is None and self._store is None:
            return {"ok": False, "layer": "episodic", "error": "no episodic store is available"}
        sid = self._session(session_id)
        try:
            episode = await asyncio.wait_for(
                asyncio.to_thread(
                    self.episodic.record,
                    text,
                    steps=list(steps or []),
                    succeeded=bool(succeeded),
                    started_at=started_at,
                ),
                timeout=self.settings.memory.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            self.stats["failures"] += 1
            self._emit("memory.failed", layer="episodic", action="episode", session_id=sid, error=repr(exc)[:200])
            return {"ok": False, "layer": "episodic", "error": f"{type(exc).__name__}: {exc}"[:300]}
        # the run that just finished is also the newest thing in working memory
        label = f"{agent}: " if agent else ""
        self.working.remember_turn(sid, f"{label}{text[:120]} → {'done' if succeeded else 'failed'}")
        self.stats["episodes"] += 1
        self.stats["writes"] += 1
        result = {
            "ok": True,
            "layer": "episodic",
            "memory_kind": "episode",
            "episode_id": str(getattr(episode, "episode_id", "")),
            "goal": str(getattr(episode, "goal", text))[:200],
            "steps": len(getattr(episode, "steps", []) or []),
            "succeeded": bool(getattr(episode, "succeeded", succeeded)),
            "session_id": sid,
        }
        self._emit("memory.updated", layer="episodic", action="episode", session_id=sid, **{
            "episode_id": result["episode_id"], "goal": result["goal"][:120],
            "steps": result["steps"], "succeeded": result["succeeded"], "agent": agent or None,
        })
        return result

    async def forget(self, memory_id: int | str, *, session_id: str = "") -> dict[str, Any]:
        """Delete one long-term record by id. Irreversible — the audit trail keeps the intent."""
        try:
            mid = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "layer": "long-term", "error": "memory_id must be an integer"}
        if self._store is None:
            return {"ok": False, "layer": "long-term", "error": "no long-term store is available"}
        sid = self._session(session_id)
        try:
            removed = bool(
                await asyncio.wait_for(asyncio.to_thread(self._store.forget, mid), timeout=self.settings.memory.timeout_s)
            )
        except Exception as exc:  # noqa: BLE001
            self.stats["failures"] += 1
            self._emit("memory.failed", layer="long-term", action="forget", session_id=sid, error=repr(exc)[:200])
            return {"ok": False, "layer": "long-term", "error": f"{type(exc).__name__}: {exc}"[:300]}
        self.stats["forgets"] += 1
        self._emit(
            "memory.updated",
            layer="long-term",
            action="forget",
            session_id=sid,
            memory_id=mid,
            removed=removed,
        )
        return {"ok": removed, "layer": "long-term", "memory_id": mid, "removed": removed}

    # ── writes (sync twins for tool handlers, which run in worker threads) ──
    def remember_now(
        self, text: str, *, kind: str = "fact", key: str | None = None, value: str | None = None, session_id: str = ""
    ) -> dict[str, Any]:
        return self._run_sync(
            lambda: self.remember(text, kind=kind, key=key, value=value, session_id=session_id),
            fallback={"ok": False, "layer": layer_of_kind(kind), "error": "memory write timed out"},
        )

    def set_preference_now(self, key: str, value: str, *, session_id: str = "") -> dict[str, Any]:
        return self._run_sync(
            lambda: self.set_preference(key, value, session_id=session_id),
            fallback={"ok": False, "layer": "preference", "error": "memory write timed out"},
        )

    def record_episode_now(
        self, goal: str, *, steps: Any = (), succeeded: bool = True, session_id: str = "", agent: str = ""
    ) -> dict[str, Any]:
        return self._run_sync(
            lambda: self.record_episode(goal, steps=steps, succeeded=succeeded, session_id=session_id, agent=agent),
            fallback={"ok": False, "layer": "episodic", "error": "memory write timed out"},
        )

    def forget_now(self, memory_id: int | str, *, session_id: str = "") -> dict[str, Any]:
        return self._run_sync(
            lambda: self.forget(memory_id, session_id=session_id),
            fallback={"ok": False, "layer": "long-term", "error": "memory write timed out"},
        )

    # ── views ───────────────────────────────────────────────────────────────
    def preferences(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._guard(lambda: self.prefs.all(limit=limit), fallback=[])

    def preference(self, key: str) -> str | None:
        return self._guard(lambda: self.prefs.get(key), fallback=None)

    def episodes(self, *, limit: int = 5) -> list[dict[str, Any]]:
        return self._guard(lambda: self.episodic.recent(limit=limit), fallback=[])

    def facts(self, *, limit: int = 5) -> list[dict[str, Any]]:
        return self._guard(lambda: self.semantic.recent(limit=limit), fallback=[])

    def patterns(self, *, limit: int = 8) -> list[dict[str, Any]]:
        return self._guard(lambda: self.procedural.top_pairs(limit=limit), fallback=[])

    def _guard(self, fn: Callable[[], Any], *, fallback: Any) -> Any:
        """Views are read from sync gateway code — a dead store must not 500 the console."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self.stats["failures"] += 1
            _log.warning("memory view failed: %s", exc)
            return fallback

    async def snapshot_async(self, query: str | None = None, *, limit: int = 20) -> dict[str, Any]:
        """``snapshot()`` without the thread hop, for async callers (the gateway)."""
        return await self._snapshot(query, limit=limit)

    def snapshot(self, query: str | None = None, *, limit: int = 20) -> dict[str, Any]:
        """Everything the ops console's *memory* tab renders — see ``console.renderMemory``."""
        return self._run_sync(
            lambda: self._snapshot(query, limit=limit),
            fallback={"ok": False, "query": query, "layers": {}, "retrieved": [], "preferences": [],
                      "error": "memory snapshot timed out"},
        )

    async def _snapshot(self, query: str | None, *, limit: int) -> dict[str, Any]:
        cfg = self.settings.memory
        text = str(query or "").strip()
        sid = self._session()
        retrieved: list[MemoryHit] = []
        trace: RetrievalTrace = RetrievalTrace(returned=0, per_layer={}, errors=[], timeouts=[])
        error: str | None = None
        if text and cfg.enabled:
            try:
                started = time.perf_counter()
                retrieved, trace = await self._retrieve(text, limit=min(int(limit or 20), 40), session_id=sid)
                self._publish_retrieval(text, retrieved, trace, started)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"[:200]

        layers: dict[str, dict[str, Any]] = {}
        for name, layer in self.layers.items():
            try:
                info = dict(layer.describe(session_id=sid) if name == "working" else layer.describe())
            except Exception as exc:  # noqa: BLE001
                info = {"count": 0, "store": "unavailable", "detail": f"{type(exc).__name__}: {exc}"[:120]}
            info.setdefault("count", 0)
            info.setdefault("store", "")
            info.setdefault("detail", "")
            probe = getattr(layer, "available", None)
            info["available"] = bool(probe()) if callable(probe) else True
            info["weight"] = self.weights.get(name, 0.0)
            layers[name] = info

        return {
            "ok": error is None,
            "name": self.name,
            "enabled": cfg.enabled,
            "query": text or None,
            "session_id": sid,
            "layers": layers,
            "retrieved": [hit.model_dump() for hit in retrieved],
            "preferences": self.preferences(limit=20),
            "episodes": self.episodes(limit=5),
            "facts": self.facts(limit=5),
            "patterns": self.patterns(limit=5),
            "working": self.working_turns(sid),
            "weights": self.weights,
            "trace": dict(trace),
            "stats": dict(self.stats),
            "store": str(self.settings.paths.memory_db) if self._store is not None else None,
            "episodes_path": str(self.settings.paths.episodes_path) if self.episodes_store is not None else None,
            "patterns_path": str(self.settings.paths.patterns_path) if self.pattern_store is not None else None,
            **({"error": error} if error else {}),
        }

    def describe(self) -> dict[str, Any]:
        """Compact self-description for ``/api/v1/info`` and the health tab."""
        cfg = self.settings.memory
        return {
            "name": self.name,
            "enabled": cfg.enabled,
            "available": self.available(),
            "layers": {
                name: {
                    "available": bool(getattr(layer, "available", lambda: True)()) if name != "working" else True,
                    "weight": self.weights.get(name, 0.0),
                    "store": str(getattr(layer, "describe", lambda: {})().get("store", ""))[:80],
                }
                for name, layer in self.layers.items()
            },
            "limits": {
                "retrieval": cfg.retrieval_limit,
                "per_layer": cfg.per_layer_limit,
                "episode_recall": cfg.episode_recall,
                "working_turns": cfg.working_turns,
                "working_sessions": cfg.working_sessions,
                "recent_scan": cfg.recent_scan,
                "timeout_s": cfg.timeout_s,
            },
            "working_delegated": self.working.delegated,
            "long_term_store": self._store is not None,
            "stats": dict(self.stats),
            "last_retrieval": {
                "query": self.last_trace.get("query", ""),
                "candidates": self.last_trace.candidates,
                "returned": self.last_trace.returned,
                "layers": self.last_trace.get("layers", []),
                "duplicates": self.last_trace.get("duplicates", 0),
                "timeouts": self.last_trace.get("timeouts", []),
                "errors": self.last_trace.get("errors", []),
            },
        }

    def health(self) -> dict[str, Any]:
        """``_slot_health`` shape: ok / degraded / off, with the reason."""
        cfg = self.settings.memory
        if not cfg.enabled:
            return {"status": "off", "detail": "STAR_MEMORY_ENABLED=false — retrieval and writes are refused"}
        alive = {
            name: bool(getattr(layer, "available", lambda: True)()) if name != "working" else True
            for name, layer in self.layers.items()
        }
        missing = sorted(name for name, ok in alive.items() if not ok)
        status = "ok" if not missing else ("degraded" if len(missing) < len(alive) else "unavailable")
        return {
            "status": status,
            "detail": {
                "layers": alive,
                "missing": missing,
                "working_delegated": self.working.delegated,
                "failures": self.stats["failures"],
                "note": "no second store: every layer adapts an existing one" if status == "ok" else
                "some layers have no backing store — retrieval continues with the rest",
            },
        }

    # ── events ──────────────────────────────────────────────────────────────
    def _emit(self, kind: str, *, layer: str, session_id: str = "", **payload: Any) -> None:
        """Publish on the shared bus. Payload keys never shadow event fields (no ``kind=``)."""
        try:
            self.bus.emit(
                kind,
                phase=EventPhase.MEMORY,
                session_id=self._session(session_id),
                layer=layer,
                **{key: value for key, value in payload.items() if value is not None},
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must never break memory
            _log.warning("memory event %s failed: %s", kind, exc)

    def _publish_retrieval(
        self, query: str, hits: Sequence[MemoryHit], trace: RetrievalTrace, started: float
    ) -> None:
        self.last_trace = trace
        self.stats["retrievals"] += 1
        self.stats["hits"] += len(hits)
        self.stats["failures"] += len(trace.get("errors") or [])
        if not str(query or "").strip() or not self.settings.memory.enabled:
            return
        self._emit(
            "memory.retrieved",
            layer=",".join(sorted({hit.layer for hit in hits})) or "none",
            query=str(query)[:160],
            candidates=trace.candidates,
            returned=len(hits),
            duplicates=trace.get("duplicates", 0),
            timeouts=",".join(trace.get("timeouts") or []) or None,
            errors=";".join(trace.get("errors") or [])[:200] or None,
            top=str(hits[0].text)[:160] if hits else None,
            top_score=round(float(hits[0].score), 4) if hits else None,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
        )


def build_memory(
    settings: Settings | None = None,
    *,
    bus: StarEventBus | None = None,
    store: Any = None,
    episodes: Any = None,
    patterns: Any = None,
    own_store: bool | None = None,
) -> MemoryManager:
    """Compose the manager. Pass the stores the app already opened — do not let it reopen them.

    ``own_store`` decides who closes the SQLite connection on shutdown; the application
    passes ``True`` because it opened the store in :func:`Backend.star.main.build_application`
    and shares it with the brain (closing twice is a no-op for ``sqlite3``).
    """
    cfg = settings or Settings()
    manager = MemoryManager(cfg, bus=bus, store=store, episodes=episodes, patterns=patterns, own_store=own_store)
    _log.info(
        "star2.memory.built enabled=%s store=%s episodes=%s patterns=%s",
        cfg.memory.enabled,
        manager.store is not None,
        manager.episodic.available(),
        manager.procedural.available(),
    )
    return manager

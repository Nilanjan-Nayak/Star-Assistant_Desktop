"""Context assembly — *memory is retrieved before the brain plans* (blueprint §7 Phase 3).

Reuses the existing long-term store (``agent.memory.store.MemoryStore``: SQLite +
hashing embeddings, ``build_context_block``, ``preferred_int/str``) instead of
inventing a second memory. Phase 8 layers working/episodic/semantic/preference
retrieval on top of this same store.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any, Deque, Protocol, runtime_checkable

from Backend.star.brain.schemas import Context, MemoryHit, UserRequest
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

_log = star_logger("context")

__all__ = [
    "ContextBuilder",
    "MemoryRetriever",
    "NullRetriever",
    "StoreRetriever",
    "time_of_day",
]


def time_of_day(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


@runtime_checkable
class MemoryRetriever(Protocol):
    """What the context builder needs from any memory implementation."""

    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    async def retrieve(self, query: str, *, limit: int = 6) -> list[MemoryHit]: ...

    async def digest(self, query: str, *, limit: int = 6) -> str: ...


class NullRetriever:
    """No memory (fresh sandbox, tests). Never fails, never invents context."""

    name = "null"

    def available(self) -> bool:
        return True

    async def retrieve(self, query: str, *, limit: int = 6) -> list[MemoryHit]:
        return []

    async def digest(self, query: str, *, limit: int = 6) -> str:
        return ""


class StoreRetriever:
    """Adapter over the existing ``MemoryStore`` (blocking SQLite → thread)."""

    name = "store"

    def __init__(self, store: Any, *, timeout_s: float = 4.0) -> None:
        self._store = store
        self._timeout_s = timeout_s

    def available(self) -> bool:
        return self._store is not None

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=self._timeout_s)

    async def retrieve(self, query: str, *, limit: int = 6) -> list[MemoryHit]:
        if not self._store or not query.strip():
            return []
        try:
            records = await self._run(self._store.recall_relevant, query, limit)
        except Exception as exc:  # noqa: BLE001 — memory must never break a reply
            _log.warning("memory retrieval failed: %s", exc)
            return []
        hits: list[MemoryHit] = []
        for record in records:
            kind = getattr(getattr(record, "kind", None), "value", None) or str(getattr(record, "kind", ""))
            layer = {"fact": "semantic", "preference": "preference", "episode": "episodic"}.get(kind, "semantic")
            hits.append(
                MemoryHit(
                    text=str(record.text),
                    layer=layer,
                    kind=kind or "fact",
                    score=float(record.score or 0.0),
                    key=getattr(record, "key", None),
                    value=getattr(record, "value", None),
                )
            )
        return hits

    async def digest(self, query: str, *, limit: int = 6) -> str:
        if not self._store or not query.strip():
            return ""
        try:
            return str(await self._run(self._store.build_context_block, query, top_k=limit) or "")
        except Exception as exc:  # noqa: BLE001
            _log.warning("memory digest failed: %s", exc)
            return ""


class ContextBuilder:
    """Builds the :class:`Context` for one request.

    Order is deliberate and observable: session scratch → long-term retrieval →
    digest. ``pipeline.StarBrain`` calls this **before** planning, and emits
    ``context.built`` so the ops console can show what the brain knew.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        retriever: MemoryRetriever | None = None,
        bus: StarEventBus | None = None,
        session_turns: int = 8,
        memory_limit: int = 6,
    ) -> None:
        self.settings = settings
        self.retriever = retriever or NullRetriever()
        self.bus = bus or StarEventBus()
        self._session_turns = session_turns
        self._memory_limit = memory_limit
        self._sessions: dict[str, Deque[str]] = {}
        self.stats = {"built": 0, "hits": 0, "failures": 0}

    # ── session scratch (working memory; formalised in Phase 8) ───────────
    def remember_turn(self, session_id: str, text: str) -> None:
        if not text.strip():
            return
        buffer = self._sessions.setdefault(session_id, deque(maxlen=self._session_turns))
        if buffer and buffer[-1] == text:
            return
        buffer.append(text)

    def working_memory(self, session_id: str) -> list[str]:
        return list(self._sessions.get(session_id, ()))

    def forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    # ── main entry ────────────────────────────────────────────────────────
    async def build(
        self,
        request: UserRequest,
        *,
        screen_summary: str | None = None,
        active_window: str | None = None,
        recent_tasks: list[dict[str, Any]] | None = None,
        predictions: list[Any] | None = None,
    ) -> Context:
        self.remember_turn(request.session_id, request.text)
        hits: list[MemoryHit] = []
        digest = ""
        try:
            hits = await self.retriever.retrieve(request.text, limit=self._memory_limit)
            digest = await self.retriever.digest(request.text, limit=self._memory_limit)
        except Exception as exc:  # noqa: BLE001
            self.stats["failures"] += 1
            _log.warning("context retrieval error: %s", exc)

        self.stats["built"] += 1
        self.stats["hits"] += len(hits)

        context = Context(
            session_id=request.session_id,
            language=request.language or self.settings.voice.default_response_language,
            time_of_day=time_of_day(),
            working_memory=self.working_memory(request.session_id),
            retrieved_memory=hits,
            memory_block=digest,
            screen_summary=screen_summary,
            active_window=active_window,
            recent_tasks=list(recent_tasks or []),
            predictions=list(predictions or []),
            dry_run=self.settings.security.dry_run,
        )
        self.bus.emit(
            "context.built",
            phase=EventPhase.BRAIN,
            request_id=request.request_id,
            session_id=request.session_id,
            memory_hits=len(hits),
            working_memory=len(context.working_memory),
            retriever=self.retriever.name,
            dry_run=context.dry_run,
            has_screen=bool(screen_summary),
        )
        return context

    def describe(self) -> dict[str, Any]:
        return {
            "retriever": self.retriever.name,
            "available": self.retriever.available(),
            "sessions": len(self._sessions),
            **self.stats,
        }

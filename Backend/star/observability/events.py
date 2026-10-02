"""``StarEvent`` — the single event vocabulary of STAR 2.0 (``star.mission.v1``).

The existing frontend README already asks for a mission event bus with this shape;
this module is that contract. Events flow: low-level ``agent.core.events.BUS`` →
:class:`Backend.star.gateway.events.AgentEventBridge` → :class:`StarEventBus` →
WebSocket subscribers (HUD mirror, ops console, tests).
"""

from __future__ import annotations

import asyncio
import enum
import itertools
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EVENT_KINDS",
    "EventPhase",
    "StarEvent",
    "StarEventBus",
    "Subscription",
    "get_event_bus",
    "new_event",
    "reset_event_bus",
]


@enum.unique
class EventPhase(str, enum.Enum):
    GATEWAY = "gateway"
    VOICE = "voice"
    BRAIN = "brain"
    PLAN = "plan"
    TASK = "task"
    TOOL = "tool"
    WORKSPACE = "workspace"
    MEMORY = "memory"
    LEARNING = "learning"
    SECURITY = "security"
    SYSTEM = "system"


# The full kind vocabulary. Adding a kind is a protocol change, so every kind the
# backend actually emits is listed here (Phase 5 made the list honest — kinds that
# were already in flight from Phases 2–4 are now official instead of flagged as
# ``_unknown_kind`` on the wire).
EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        # ── Phase 2: voice ────────────────────────────────────────────────
        "voice.config",
        "voice.status",
        # ── Phase 3: brain ────────────────────────────────────────────────
        "brain.turn_started",
        "brain.turn_completed",
        "reasoning.complete",
        "plan.executed",
        "prediction.proposed",
        # ── Phase 4: tools + security ─────────────────────────────────────
        "tool.call",
        "tool.decision",
        "tool.registered",
        "tool.gate_installed",
        "tool.gate_removed",
        "audit.recorded",
        "security.ready",
        "security.decision",
        "security.confirmation_requested",
        "security.confirmation_resolved",
        "security.confirmation_expired",
        "security.stop",
        "security.resume",
        "security.budget_exceeded",
        "security.secret_detected",
        # ── Phase 5: agents (browser agent is the first one) ──────────────
        "agent.started",
        "agent.step",
        "agent.completed",
        "agent.failed",
        # ── gateway lifecycle ─────────────────────────────────────────────
        "server.stop",
        "session.started",
        "session.closed",
        "request.received",
        "language.detected",
        "context.built",
        "plan.created",
        "plan.rejected",
        "plan.paused",
        "plan.resumed",
        "plan.checkpoint",
        "task.started",
        "task.progress",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "task.recovered",
        "tool.called",
        "tool.result",
        "confirmation.requested",
        "confirmation.resolved",
        "confirmation.expired",
        "verification.result",
        "reflection.result",
        "prediction.made",
        "memory.updated",
        "memory.retrieved",
        "memory.failed",        # Phase 8: a layer/store refused or timed out — reported, never hidden
        "pattern.learned",
        # ── Phase 9: the safe learning loop ─────────────────────────────────
        "learning.observed",    # a finished run/turn was turned into candidate pairs
        "learning.feedback",    # explicit user feedback (positive/negative/correction)
        "learning.validated",   # a validation sweep ran (considered/validated/rejected/promoted)
        "learning.rejected",    # a candidate failed a gate — reason recorded, never hidden
        "learning.promoted",    # a validated candidate was written into a real store
        "safety.blocked",
        "workspace.created",
        "workspace.destroyed",
        "workspace.checkpoint",
        # Phase 7 emitted these already; Phase 8 made the vocabulary honest again
        "workspace.closed",
        "workspace.restored",
        "workspace.expired",
        "workspace.blocked",
        "workspace.write",
        "workspace.used",
        "workspace.spawn",
        "response.spoken",
        "emergency.stop",
        "agent.ready",
        "health.changed",
    }
)


class StarEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = 0
    event_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: str
    phase: EventPhase = EventPhase.SYSTEM
    session_id: str = "star-default"
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


_counter = itertools.count(1)


def new_event(
    kind: str,
    *,
    phase: EventPhase = EventPhase.SYSTEM,
    session_id: str = "star-default",
    correlation_id: str | None = None,
    **payload: Any,
) -> StarEvent:
    """Build a validated event. Unknown kinds are accepted but flagged."""
    if kind not in EVENT_KINDS:
        payload.setdefault("_unknown_kind", True)
    return StarEvent(
        event_id=f"se_{next(_counter):08x}",
        kind=kind,
        phase=phase,
        session_id=session_id,
        correlation_id=correlation_id,
        payload=payload,
    )


class Subscription:
    """A subscriber's queue + its ``close()`` handle."""

    def __init__(self, bus: StarEventBus, queue: asyncio.Queue[StarEvent], kinds: frozenset[str] | None) -> None:
        self.bus = bus
        self.queue = queue
        self.kinds = kinds
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.bus._remove(self)  # noqa: SLF001 — intentional internal handshake

    async def __aiter__(self):  # type: ignore[override]
        while not self.closed:
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=30.0)
            except TimeoutError:
                yield new_event("health.changed", phase=EventPhase.GATEWAY, alive=True)
                continue
            yield event


type EventListener = Callable[[StarEvent], Awaitable[None] | None]


class StarEventBus:
    """Fan-out bus with a bounded replay ring buffer.

    * ``publish`` is sync and never blocks (full subscriber queues drop their
      oldest event and count the drop — a slow consumer must not stall the brain).
    * ``publish_threadsafe`` exists for code running on another loop/thread
      (e.g. the legacy ``AgentBridge`` daemon loop).
    """

    def __init__(self, *, history_size: int = 500) -> None:
        self._subs: list[Subscription] = []
        self._listeners: list[EventListener] = []
        self._history: deque[StarEvent] = deque(maxlen=max(1, history_size))
        self._seq = 0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped = 0

    # ── wiring ──────────────────────────────────────────────────────────────
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()

    def add_listener(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def subscribe(self, *, kinds: frozenset[str] | None = None, maxsize: int = 256) -> Subscription:
        sub = Subscription(self, asyncio.Queue(maxsize=maxsize), kinds)
        self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    # ── publishing ──────────────────────────────────────────────────────────
    def publish(self, event: StarEvent) -> StarEvent:
        with self._lock:
            self._seq += 1
            stamped = event.model_copy(update={"seq": self._seq}) if event.seq == 0 else event
            self._history.append(stamped)
        for sub in list(self._subs):
            if sub.closed or (sub.kinds is not None and stamped.kind not in sub.kinds):
                continue
            try:
                sub.queue.put_nowait(stamped)
            except asyncio.QueueFull:
                try:
                    _ = sub.queue.get_nowait()
                    sub.queue.put_nowait(stamped)
                    self.dropped += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    self.dropped += 1
        for listener in list(self._listeners):
            try:
                result = listener(stamped)
                if asyncio.iscoroutine(result):
                    _schedule(result)
            except Exception:  # noqa: BLE001 — a bad listener must never break the bus
                pass
        return stamped

    def publish_threadsafe(self, event: StarEvent) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self.publish(event)
            return
        try:
            loop.call_soon_threadsafe(self.publish, event)
        except RuntimeError:  # pragma: no cover - loop shut down mid-publish
            pass

    def emit(self, kind: str, *, phase: EventPhase = EventPhase.SYSTEM, **payload: Any) -> StarEvent:
        return self.publish(new_event(kind, phase=phase, **payload))

    # ── introspection ───────────────────────────────────────────────────────
    def history(
        self,
        *,
        limit: int = 50,
        kinds: frozenset[str] | None = None,
        since_seq: int = 0,
    ) -> list[StarEvent]:
        with self._lock:
            items = list(self._history)
        if kinds is not None:
            items = [event for event in items if event.kind in kinds]
        if since_seq:
            items = [event for event in items if event.seq > since_seq]
        return items[-max(0, limit) :]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for event in self._history:
                counts[event.kind] = counts.get(event.kind, 0) + 1
            return {
                "seq": self._seq,
                "buffered": len(self._history),
                "subscribers": len(self._subs),
                "dropped": self.dropped,
                "kind_counts": counts,
            }


def _schedule(coro: Awaitable[Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(_run(coro))


async def _run(coro: Awaitable[Any]) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        pass


_BUS: StarEventBus | None = None


def get_event_bus(*, history_size: int = 500) -> StarEventBus:
    global _BUS
    if _BUS is None:
        _BUS = StarEventBus(history_size=history_size)
    return _BUS


def reset_event_bus() -> None:
    """Test helper: drop the global bus (and all its subscribers)."""
    global _BUS
    if _BUS is not None:
        for sub in list(_BUS._subs):  # noqa: SLF001
            sub.close()
    _BUS = None

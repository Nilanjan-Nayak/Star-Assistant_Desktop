"""Bridge: low-level ``agent.core.events.BUS`` → STAR 2.0 ``StarEventBus``.

The agent core already publishes 8 typed events (action/skill/episode/state/
safety). Re-publishing them as :class:`StarEvent`s is what lets the HUD, the ops
console and the audit trail all watch the *same* stream without the frontend ever
importing agent internals (blueprint rule: keep agent internals out of the UI).
"""

from __future__ import annotations

from typing import Any

from agent.core.events import (
    BUS,
    ActionCompletedEvent,
    ActionStartedEvent,
    EpisodeEndedEvent,
    EpisodeStartedEvent,
    SafetyBlockedEvent,
    SkillCompletedEvent,
    SkillStartedEvent,
    StateChangedEvent,
)

from Backend.star.observability.events import EventPhase, StarEventBus, get_event_bus, new_event

__all__ = ["AgentEventBridge", "agent_event_to_star"]

_KIND_MAP: dict[str, tuple[str, EventPhase]] = {
    "action.started": ("tool.called", EventPhase.TOOL),
    "action.completed": ("tool.result", EventPhase.TOOL),
    "safety.blocked": ("safety.blocked", EventPhase.SECURITY),
    "state.changed": ("task.progress", EventPhase.TASK),
    "episode.started": ("task.started", EventPhase.TASK),
    "episode.ended": ("task.completed", EventPhase.TASK),
    "skill.started": ("tool.called", EventPhase.TOOL),
    "skill.completed": ("tool.result", EventPhase.TOOL),
}


def agent_event_to_star(event: Any, *, session_id: str = "star-default") -> Any:
    """Translate one agent event into a Star event (pure function, unit-testable)."""
    kind = getattr(event, "kind", "")
    star_kind, phase = _KIND_MAP.get(kind, ("task.progress", EventPhase.SYSTEM))
    payload = event.model_dump(mode="json", exclude={"event_id", "kind", "timestamp"})
    if kind == "episode.ended":
        state = str(payload.get("state", ""))
        star_kind = "task.completed" if state == "succeeded" else "task.failed"
    if kind == "safety.blocked":
        phase = EventPhase.SECURITY
    return new_event(
        star_kind,
        phase=phase,
        session_id=session_id,
        source="agent.core",
        agent_kind=kind,
        **payload,
    )


class AgentEventBridge:
    """Subscribe/unsubscribe helper. ``attach()`` is idempotent."""

    def __init__(self, bus: StarEventBus | None = None, *, session_id: str = "star-default") -> None:
        self.bus = bus or get_event_bus()
        self.session_id = session_id
        self._unsubs: list[Any] = []
        self.forwarded = 0

    def attach(self) -> AgentEventBridge:
        if self._unsubs:
            return self
        for event_type in (
            ActionStartedEvent,
            ActionCompletedEvent,
            SafetyBlockedEvent,
            StateChangedEvent,
            EpisodeStartedEvent,
            EpisodeEndedEvent,
            SkillStartedEvent,
            SkillCompletedEvent,
        ):
            self._unsubs.append(BUS.subscribe(event_type, self._make_handler()))
        return self

    def detach(self) -> None:
        for unsubscribe in self._unsubs:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()

    def _make_handler(self) -> Any:
        async def _handler(event: Any) -> None:
            self.forwarded += 1
            self.bus.publish_threadsafe(agent_event_to_star(event, session_id=self.session_id))

        return _handler

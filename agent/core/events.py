"""Typed event bus with a Pydantic discriminated-union payload."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agent.core.enums import ActionKind, AgentState
from agent.core.ids import ActionId, EpisodeId, EventId, SkillName, new_event_id
from agent.core.logging import get_logger

_log = get_logger("agent.events")


class EventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: EventId = Field(default_factory=new_event_id)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ActionStartedEvent(EventBase):
    kind: Literal["action.started"] = "action.started"
    action_id: ActionId
    action_kind: ActionKind


class ActionCompletedEvent(EventBase):
    kind: Literal["action.completed"] = "action.completed"
    action_id: ActionId
    success: bool
    duration_ms: float


class SafetyBlockedEvent(EventBase):
    kind: Literal["safety.blocked"] = "safety.blocked"
    reason: str
    action: str


class StateChangedEvent(EventBase):
    kind: Literal["state.changed"] = "state.changed"
    from_state: AgentState
    to_state: AgentState


class EpisodeStartedEvent(EventBase):
    kind: Literal["episode.started"] = "episode.started"
    episode_id: EpisodeId
    goal: str


class EpisodeEndedEvent(EventBase):
    kind: Literal["episode.ended"] = "episode.ended"
    episode_id: EpisodeId
    state: AgentState
    steps: int


class SkillStartedEvent(EventBase):
    kind: Literal["skill.started"] = "skill.started"
    skill: SkillName


class SkillCompletedEvent(EventBase):
    kind: Literal["skill.completed"] = "skill.completed"
    skill: SkillName
    ok: bool
    duration_ms: float


Event = Annotated[
    ActionStartedEvent
    | ActionCompletedEvent
    | SafetyBlockedEvent
    | StateChangedEvent
    | EpisodeStartedEvent
    | EpisodeEndedEvent
    | SkillStartedEvent
    | SkillCompletedEvent,
    Field(discriminator="kind"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

type EventHandler[E: EventBase] = Callable[[E], Awaitable[None]]


class EventBus:
    """Async pub-sub with per-type subscription. ``subscribe`` returns an unsubscribe fn."""

    def __init__(self) -> None:
        self._subs: dict[type[EventBase], list[EventHandler[EventBase]]] = defaultdict(list)

    def subscribe[E: EventBase](
        self,
        event_type: type[E],
        handler: EventHandler[E],
    ) -> Callable[[], None]:
        typed: EventHandler[EventBase] = handler  # type: ignore[assignment]
        self._subs[event_type].append(typed)

        def _unsubscribe() -> None:
            handlers = self._subs.get(event_type)
            if handlers is None:
                return
            try:
                handlers.remove(typed)
            except ValueError:
                return

        return _unsubscribe

    async def publish(self, event: EventBase) -> None:
        for etype, handlers in list(self._subs.items()):
            if not isinstance(event, etype):
                continue
            for handler in list(handlers):
                try:
                    await handler(event)
                except Exception:
                    _log.exception(
                        "handler failed",
                        extra={"extra_ctx": {"event_type": etype.__name__}},
                    )

    def clear(self) -> None:
        self._subs.clear()


BUS: EventBus = EventBus()

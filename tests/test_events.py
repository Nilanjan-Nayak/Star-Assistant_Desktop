from __future__ import annotations

import pytest

from agent.core.enums import ActionKind, AgentState
from agent.core.events import ActionStartedEvent, EventBus, StateChangedEvent
from agent.core.ids import new_action_id


@pytest.mark.asyncio
async def test_subscribe_and_publish() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_action(event: ActionStartedEvent) -> None:
        seen.append(event.kind)

    async def on_state(event: StateChangedEvent) -> None:
        seen.append(f"{event.from_state.value}->{event.to_state.value}")

    unsub = bus.subscribe(ActionStartedEvent, on_action)
    bus.subscribe(StateChangedEvent, on_state)

    await bus.publish(
        ActionStartedEvent(action_id=new_action_id(), action_kind=ActionKind.CLICK)
    )
    await bus.publish(
        StateChangedEvent(from_state=AgentState.IDLE, to_state=AgentState.PERCEIVING)
    )
    assert seen == ["action.started", "idle->perceiving"]
    unsub()
    await bus.publish(
        ActionStartedEvent(action_id=new_action_id(), action_kind=ActionKind.CLICK)
    )
    assert seen == ["action.started", "idle->perceiving"]

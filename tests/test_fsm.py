from __future__ import annotations

import pytest

from agent.core.enums import AgentState
from agent.core.errors import StateTransitionError
from agent.planning.state_machine import StateMachine


@pytest.mark.asyncio
async def test_happy_path() -> None:
    fsm = StateMachine()
    assert fsm.state is AgentState.IDLE
    await fsm.transition(AgentState.PERCEIVING)
    await fsm.transition(AgentState.PLANNING)
    await fsm.transition(AgentState.ACTING)
    await fsm.transition(AgentState.VERIFYING)
    await fsm.transition(AgentState.REFLECTING)
    await fsm.transition(AgentState.SUCCEEDED)


@pytest.mark.asyncio
async def test_illegal_transition() -> None:
    fsm = StateMachine()
    with pytest.raises(StateTransitionError):
        await fsm.transition(AgentState.ACTING)


@pytest.mark.asyncio
async def test_terminal_only_to_idle() -> None:
    fsm = StateMachine(AgentState.SUCCEEDED)
    await fsm.transition(AgentState.IDLE)
    fsm = StateMachine(AgentState.FAILED)
    with pytest.raises(StateTransitionError):
        await fsm.transition(AgentState.PLANNING)

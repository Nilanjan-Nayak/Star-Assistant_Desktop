"""Runtime FSM with an exhaustive, frozen transition table.

Illegal transitions raise ``StateTransitionError``. Terminal states may only
return to ``IDLE``. The table is data, not a chain of ifs, so adding a state
without listing successors is a ``KeyError`` at construction — caught by tests.
"""

from __future__ import annotations

from typing import Final, Mapping

from agent.core.enums import TERMINAL_STATES, AgentState
from agent.core.errors import StateTransitionError
from agent.core.events import BUS, StateChangedEvent

_VALID: Final[Mapping[AgentState, frozenset[AgentState]]] = {
    AgentState.IDLE: frozenset({AgentState.PERCEIVING, AgentState.ABORTED}),
    AgentState.PERCEIVING: frozenset(
        {AgentState.PLANNING, AgentState.FAILED, AgentState.ABORTED}
    ),
    AgentState.PLANNING: frozenset(
        {
            AgentState.ACTING,
            AgentState.SUCCEEDED,
            AgentState.BLOCKED,
            AgentState.ESCALATED,
            AgentState.FAILED,
            AgentState.ABORTED,
        }
    ),
    AgentState.ACTING: frozenset(
        {AgentState.VERIFYING, AgentState.BLOCKED, AgentState.FAILED, AgentState.ABORTED}
    ),
    AgentState.VERIFYING: frozenset(
        {AgentState.REFLECTING, AgentState.FAILED, AgentState.ABORTED}
    ),
    AgentState.REFLECTING: frozenset(
        {AgentState.PERCEIVING, AgentState.SUCCEEDED, AgentState.FAILED, AgentState.ABORTED}
    ),
    AgentState.BLOCKED: frozenset(
        {AgentState.ESCALATED, AgentState.ABORTED, AgentState.IDLE}
    ),
    AgentState.ESCALATED: frozenset(
        {AgentState.PERCEIVING, AgentState.ABORTED, AgentState.IDLE}
    ),
    AgentState.SUCCEEDED: frozenset({AgentState.IDLE}),
    AgentState.FAILED: frozenset({AgentState.IDLE}),
    AgentState.ABORTED: frozenset({AgentState.IDLE}),
}


def _assert_table_complete() -> None:
    missing = [state for state in AgentState if state not in _VALID]
    if missing:
        raise RuntimeError(f"FSM table missing states: {missing}")
    for state in TERMINAL_STATES:
        extra = _VALID[state] - {AgentState.IDLE}
        if extra:
            raise RuntimeError(f"terminal {state} has non-IDLE successors: {extra}")


_assert_table_complete()


class StateMachine:
    def __init__(self, initial: AgentState = AgentState.IDLE) -> None:
        self._state = initial

    @property
    def state(self) -> AgentState:
        return self._state

    def can_transition(self, to: AgentState) -> bool:
        return to in _VALID[self._state]

    async def transition(self, to: AgentState) -> None:
        if to not in _VALID[self._state]:
            raise StateTransitionError(
                f"illegal transition {self._state.value} → {to.value}",
                from_state=self._state.value,
                to_state=to.value,
            )
        prev, self._state = self._state, to
        await BUS.publish(StateChangedEvent(from_state=prev, to_state=to))

    def reset(self) -> None:
        self._state = AgentState.IDLE

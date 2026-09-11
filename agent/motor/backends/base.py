"""``MotorBackend`` protocol — swap pyautogui for tests / other backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.motor.spec import ActionSpec


@runtime_checkable
class MotorBackend(Protocol):
    """Executes a single physical action. Raises ``MotorError`` on failure."""

    async def execute(self, spec: ActionSpec) -> None: ...

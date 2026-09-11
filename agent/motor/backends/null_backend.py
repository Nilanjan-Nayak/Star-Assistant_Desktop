"""No-op backend for dry-run and tests."""

from __future__ import annotations

from agent.motor.spec import ActionSpec


class NullBackend:
    """Records actions without executing them."""

    def __init__(self) -> None:
        self.executed: list[ActionSpec] = []

    async def execute(self, spec: ActionSpec) -> None:
        self.executed.append(spec)

    def reset(self) -> None:
        self.executed.clear()

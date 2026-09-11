"""Human-in-the-loop approval protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.motor.spec import ActionSpec


@runtime_checkable
class Approver(Protocol):
    async def approve(self, prompt: str, spec: ActionSpec) -> bool: ...


class AutoApprove:
    """Tests / permissive mode — always says yes."""

    async def approve(self, prompt: str, spec: ActionSpec) -> bool:
        _ = (prompt, spec)
        return True


class AutoDeny:
    """Paranoid / CI — always says no."""

    async def approve(self, prompt: str, spec: ActionSpec) -> bool:
        _ = (prompt, spec)
        return False


class ConsoleApprover:
    """Blocking stdin prompt. Not used in tests."""

    async def approve(self, prompt: str, spec: ActionSpec) -> bool:
        try:
            answer = input(f"{prompt} [{spec.kind.value}] (y/N): ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

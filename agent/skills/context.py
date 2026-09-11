"""SkillContext protocol — what a skill sees, including a gated ``act()``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent.core.cancel import CancellationToken
from agent.core.ids import CapToken
from agent.motor.controller import MotorController
from agent.motor.result import ActionResult
from agent.motor.spec import ActionSpec
from agent.perception.cascade import PerceptionCascade
from agent.safety.governor import SafetyGovernor
from agent.world.model import WorldModel


@runtime_checkable
class SkillContext(Protocol):
    motor: MotorController
    perception: PerceptionCascade
    world: WorldModel
    governor: SafetyGovernor

    async def act(
        self,
        spec: ActionSpec,
        *,
        capability: CapToken | None = None,
        token: CancellationToken | None = None,
    ) -> ActionResult: ...


@dataclass(frozen=True, slots=True)
class DefaultSkillContext:
    motor: MotorController
    perception: PerceptionCascade
    world: WorldModel
    governor: SafetyGovernor

    async def act(
        self,
        spec: ActionSpec,
        *,
        capability: CapToken | None = None,
        token: CancellationToken | None = None,
    ) -> ActionResult:
        """Every physical action a skill takes must pass the governor."""
        await self.governor.check(spec, capability=capability)
        result = await self.motor.execute(spec, token=token)
        self.governor.log(result)
        return result

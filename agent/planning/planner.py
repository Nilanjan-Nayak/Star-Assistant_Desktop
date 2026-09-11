from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.planning.memory import EpisodicMemory
from agent.planning.step import PlanStep
from agent.world.model import WorldModel


@runtime_checkable
class Planner(Protocol):
    async def next_step(
        self,
        goal: str,
        history: list[PlanStep],
        world: WorldModel,
        memory: EpisodicMemory,
    ) -> PlanStep | None: ...

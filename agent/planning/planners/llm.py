from __future__ import annotations

from agent.memory.store import MemoryStore
from agent.planning.memory import EpisodicMemory
from agent.planning.planners.stub import StubPlanner
from agent.planning.step import PlanStep
from agent.world.model import WorldModel


class LLMPlanner:
    """Anthropic-backed planner (wired stub — falls back so the code is runnable).

    When a real LLM is wired, ``semantic.build_context_block(goal)`` is the
    block that gets stuffed into the system prompt. Until then the stub
    planner already consumes structured preferences.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        semantic: MemoryStore | None = None,
    ) -> None:
        self.model = model
        self.semantic = semantic
        self._fallback = StubPlanner(semantic=semantic)

    async def next_step(
        self,
        goal: str,
        history: list[PlanStep],
        world: WorldModel,
        memory: EpisodicMemory,
    ) -> PlanStep | None:
        _ = self.semantic.build_context_block(goal) if self.semantic is not None else ""
        return await self._fallback.next_step(goal, history, world, memory)

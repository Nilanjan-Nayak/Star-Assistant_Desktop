from __future__ import annotations

import pytest

from agent.planning.memory import EpisodicMemory
from agent.planning.planners.stub import StubPlanner
from agent.world.model import WorldModel


@pytest.mark.asyncio
async def test_stub_youtube(fake_world: WorldModel, tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    planner = StubPlanner()
    memory = EpisodicMemory(path=tmp_path / "ep.jsonl")
    step = await planner.next_step("play a youtube video for cats", [], fake_world, memory)
    assert step is not None
    assert step.skill == "youtube"
    assert "cats" in str(step.params["query"])


@pytest.mark.asyncio
async def test_stub_done_returns_none(fake_world: WorldModel, tmp_path: object) -> None:
    from pathlib import Path

    from agent.core.ids import parse_skill_name
    from agent.planning.step import PlanStep

    assert isinstance(tmp_path, Path)
    planner = StubPlanner()
    memory = EpisodicMemory(path=tmp_path / "ep.jsonl")
    done = PlanStep(
        thought="done",
        skill=parse_skill_name("volume"),
        params={"level": 70},
        success=True,
    )
    step = await planner.next_step("set volume", [done], fake_world, memory)
    assert step is None

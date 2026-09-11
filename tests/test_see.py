from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.enums import AgentState
from agent.core.ids import parse_skill_name
from agent.memory.store import MemoryStore
from agent.motor.backends.null_backend import NullBackend
from agent.motor.controller import MotorController
from agent.perception.cascade import PerceptionCascade
from agent.perception.ocr_engine import OcrToken, ScriptedOcr, group_lines
from agent.perception.query import PerceptionQuery
from agent.perception.strategies.ocr import OCRPerception
from agent.planning.memory import EpisodicMemory
from agent.planning.planners.stub import StubPlanner
from agent.planning.react_agent import ReActAgent
from agent.safety.config import GovernorConfig
from agent.safety.governor import SafetyGovernor
from agent.skills.context import DefaultSkillContext
from agent.skills.registry import SkillRegistry
from agent.world.model import WorldModel


def test_group_lines() -> None:
    tokens = [
        OcrToken("Hello", 10, 10, 40, 12, 0.9),
        OcrToken("World", 60, 11, 40, 12, 0.9),
        OcrToken("Next", 10, 40, 30, 12, 0.8),
    ]
    assert group_lines(tokens) == ["Hello World", "Next"]


@pytest.mark.asyncio
async def test_ocr_find_filters_scripted() -> None:
    engine = ScriptedOcr(
        [
            OcrToken("Search", 20, 20, 80, 16, 0.95),
            OcrToken("Settings", 20, 60, 80, 16, 0.9),
        ]
    )
    ocr = OCRPerception(engine=engine)
    from agent.geometry.raster import solid

    hits = await ocr.find(solid(200, 100), PerceptionQuery(text="search", min_confidence=0.2))
    assert hits
    assert hits[0].text == "Search"


@pytest.mark.asyncio
async def test_see_skill_reads_screen(fake_world: WorldModel, tmp_path: Path) -> None:
    ocr = OCRPerception(
        engine=ScriptedOcr(
            [
                OcrToken("Hello", 8, 8, 40, 12, 0.95),
                OcrToken("Nilanjan", 52, 8, 70, 12, 0.92),
            ]
        )
    )
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    gov = SafetyGovernor(
        GovernorConfig(audit_path=str(tmp_path / "audit.jsonl"), dry_run=True)
    )
    ctx = DefaultSkillContext(
        motor=motor,
        perception=PerceptionCascade(strategies=[ocr]),
        world=fake_world,
        governor=gov,
    )
    registry = SkillRegistry(ctx)
    result = await registry.run(
        parse_skill_name("see"),
        {"query": "*", "save_path": str(tmp_path / "see.png")},
    )
    assert result.ok
    assert "Hello" in str(result.data.get("text"))
    assert (tmp_path / "see.png").exists()
    assert result.data.get("engine") == "scripted"


@pytest.mark.asyncio
async def test_planner_routes_dakho(fake_world: WorldModel, tmp_path: Path) -> None:
    planner = StubPlanner()
    memory = EpisodicMemory(path=tmp_path / "ep.jsonl")
    step = await planner.next_step("screen dakho", [], fake_world, memory)
    assert step is not None
    assert step.skill == "see"


@pytest.mark.asyncio
async def test_see_episode(fake_world: WorldModel, tmp_path: Path) -> None:
    ocr = OCRPerception(engine=ScriptedOcr([OcrToken("OK", 4, 4, 20, 10, 0.99)]))
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    gov = SafetyGovernor(
        GovernorConfig(audit_path=str(tmp_path / "audit.jsonl"), dry_run=True)
    )
    ctx = DefaultSkillContext(
        motor=motor,
        perception=PerceptionCascade(strategies=[ocr]),
        world=fake_world,
        governor=gov,
    )
    agent = ReActAgent(
        registry=SkillRegistry(ctx),
        motor=motor,
        world=fake_world,
        governor=gov,
        memory=EpisodicMemory(path=tmp_path / "ep.jsonl"),
        semantic=MemoryStore(tmp_path / "mem.db"),
        max_steps=5,
    )
    try:
        episode = await agent.run("look at the screen")
        assert episode.final_state is AgentState.SUCCEEDED
        assert episode.steps[0].skill == "see"
        assert episode.steps[0].success is True
    finally:
        if agent.semantic is not None:
            agent.semantic.close()

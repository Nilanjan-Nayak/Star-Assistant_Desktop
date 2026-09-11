from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.enums import AgentState
from agent.motor.backends.null_backend import NullBackend
from agent.motor.controller import MotorController
from agent.perception.cascade import PerceptionCascade
from agent.planning.memory import EpisodicMemory
from agent.planning.react_agent import ReActAgent
from agent.safety.config import GovernorConfig
from agent.safety.governor import SafetyGovernor
from agent.skills.context import DefaultSkillContext
from agent.skills.registry import SkillRegistry
from agent.memory.store import MemoryStore
from agent.world.model import WorldModel


@pytest.mark.asyncio
async def test_volume_episode(fake_world: WorldModel, tmp_path: Path) -> None:
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    gov = SafetyGovernor(
        GovernorConfig(audit_path=str(tmp_path / "audit.jsonl"), dry_run=True)
    )
    ctx = DefaultSkillContext(
        motor=motor,
        perception=PerceptionCascade(strategies=[]),
        world=fake_world,
        governor=gov,
    )
    registry = SkillRegistry(ctx)
    agent = ReActAgent(
        registry=registry,
        motor=motor,
        world=fake_world,
        governor=gov,
        memory=EpisodicMemory(path=tmp_path / "ep.jsonl"),
        max_steps=5,
    )
    episode = await agent.run("set the volume")
    assert episode.final_state is AgentState.SUCCEEDED
    assert episode.steps
    assert episode.steps[0].skill == "volume"
    assert episode.steps[0].success is True


@pytest.mark.asyncio
async def test_empty_goal_rejected(fake_world: WorldModel, tmp_path: Path) -> None:
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    gov = SafetyGovernor(GovernorConfig(audit_path=str(tmp_path / "audit.jsonl")))
    ctx = DefaultSkillContext(
        motor=motor,
        perception=PerceptionCascade(strategies=[]),
        world=fake_world,
        governor=gov,
    )
    agent = ReActAgent(
        registry=SkillRegistry(ctx),
        motor=motor,
        world=fake_world,
        governor=gov,
        memory=EpisodicMemory(path=tmp_path / "ep.jsonl"),
    )
    with pytest.raises(ValueError):
        await agent.run("   ")


@pytest.mark.asyncio
async def test_volume_preference_is_remembered(
    fake_world: WorldModel, tmp_path: Path
) -> None:
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    gov = SafetyGovernor(
        GovernorConfig(audit_path=str(tmp_path / "audit.jsonl"), dry_run=True)
    )
    ctx = DefaultSkillContext(
        motor=motor,
        perception=PerceptionCascade(strategies=[]),
        world=fake_world,
        governor=gov,
    )
    store = MemoryStore(tmp_path / "mem.db")
    agent = ReActAgent(
        registry=SkillRegistry(ctx),
        motor=motor,
        world=fake_world,
        governor=gov,
        memory=EpisodicMemory(path=tmp_path / "ep.jsonl"),
        semantic=store,
        max_steps=5,
    )
    first = await agent.run("set volume to 40")
    assert first.steps[0].params["level"] == 40
    agent.fsm.reset()
    second = await agent.run("set the volume")
    assert second.steps[0].params["level"] == 40
    store.close()

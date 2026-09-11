from __future__ import annotations

import pytest

from agent.core.ids import parse_skill_name
from agent.core.errors import SkillNotFound
from agent.motor.backends.null_backend import NullBackend
from agent.motor.controller import MotorController
from agent.perception.cascade import PerceptionCascade
from agent.safety.governor import SafetyGovernor
from agent.skills.context import DefaultSkillContext
from agent.skills.registry import SkillRegistry
from agent.world.model import WorldModel


@pytest.fixture
def registry(fake_world: WorldModel) -> SkillRegistry:
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    ctx = DefaultSkillContext(
        motor=motor,
        perception=PerceptionCascade(strategies=[]),
        world=fake_world,
        governor=SafetyGovernor(),
    )
    return SkillRegistry(ctx)


@pytest.mark.asyncio
async def test_volume_validates_range(registry: SkillRegistry) -> None:
    result = await registry.run(parse_skill_name("volume"), {"level": 150})
    assert result.ok is False
    assert result.error is not None
    assert "param validation" in result.error


@pytest.mark.asyncio
async def test_unknown_skill(registry: SkillRegistry) -> None:
    with pytest.raises(SkillNotFound):
        await registry.run(parse_skill_name("nope"), {})


@pytest.mark.asyncio
async def test_describe_lists_builtins(registry: SkillRegistry) -> None:
    names = {str(item["name"]) for item in registry.describe()}
    assert {"volume", "brightness", "screenshot", "launch", "youtube"} <= names


@pytest.mark.asyncio
async def test_launch_blocks_shell(registry: SkillRegistry) -> None:
    result = await registry.run(parse_skill_name("launch"), {"app": "bash"})
    assert result.ok is False

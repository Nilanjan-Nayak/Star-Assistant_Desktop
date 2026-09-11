from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.enums import SafetyLevel
from agent.facade import ComputerControlAgent
from agent.safety.config import GovernorConfig
from agent.world.model import WorldModel


@pytest.mark.asyncio
async def test_dry_run_describe(fake_world: WorldModel, tmp_path: Path) -> None:
    agent = ComputerControlAgent(
        governor_config=GovernorConfig.for_level(SafetyLevel.NORMAL, dry_run=True),
        dry_run=True,
        world=fake_world,
        memory_path=tmp_path / "mem.db",
    )
    names = {str(item["name"]) for item in agent.registry.describe()}
    assert "volume" in names
    health = agent.health()
    assert health.as_dict()["status"] in {"ok", "degraded"}
    agent.semantic.close()


def test_config_presets() -> None:
    paranoid = GovernorConfig.for_level(SafetyLevel.PARANOID)
    assert paranoid.require_capability is True
    assert paranoid.max_total_actions == 50
    permissive = GovernorConfig.for_level(SafetyLevel.PERMISSIVE)
    assert permissive.max_actions_per_minute == 300

from __future__ import annotations

import pytest

from agent.core.errors import BudgetExhausted, SafetyViolation
from agent.geometry.bbox import BoundingBox
from agent.motor.spec import click, type_text, wait
from agent.safety.config import GovernorConfig
from agent.safety.governor import SafetyGovernor
from agent.safety.human import AutoApprove


@pytest.mark.asyncio
async def test_total_budget_enforced() -> None:
    gov = SafetyGovernor(GovernorConfig(max_total_actions=2))
    gov.begin_episode()
    spec = click(10, 10)
    await gov.check(spec)
    await gov.check(spec)
    with pytest.raises(BudgetExhausted):
        await gov.check(spec)


@pytest.mark.asyncio
async def test_budget_not_consumed_when_later_invariant_fails() -> None:
    gov = SafetyGovernor(
        GovernorConfig(
            max_total_actions=1,
            forbidden_regions=[BoundingBox(x=0, y=0, width=100, height=100)],
        )
    )
    gov.begin_episode()
    with pytest.raises(SafetyViolation):
        await gov.check(click(50, 50))
    # The failed check must not consume the budget.
    await gov.check(wait(0.1))


@pytest.mark.asyncio
async def test_forbidden_region_blocks_click() -> None:
    gov = SafetyGovernor(
        GovernorConfig(forbidden_regions=[BoundingBox(x=0, y=0, width=100, height=100)])
    )
    gov.begin_episode()
    with pytest.raises(SafetyViolation):
        await gov.check(click(50, 50))


@pytest.mark.asyncio
async def test_keyword_blocked() -> None:
    gov = SafetyGovernor(GovernorConfig())
    gov.begin_episode()
    with pytest.raises(SafetyViolation):
        await gov.check(type_text("rm -rf /"))


@pytest.mark.asyncio
async def test_approver_can_override() -> None:
    gov = SafetyGovernor(GovernorConfig(), approver=AutoApprove())
    gov.begin_episode()
    await gov.check(type_text("rm -rf /"))


@pytest.mark.asyncio
async def test_rate_limit() -> None:
    gov = SafetyGovernor(GovernorConfig(max_actions_per_minute=2, max_total_actions=100))
    gov.begin_episode()
    await gov.check(wait(0.0))
    await gov.check(wait(0.0))
    with pytest.raises(SafetyViolation):
        await gov.check(wait(0.0))

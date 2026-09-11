"""Generic typed ``Skill[TIn]`` ABC."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import ClassVar, cast

from pydantic import BaseModel, ValidationError

from agent.core.enums import ActionKind
from agent.core.errors import AgentError, PreconditionFailed
from agent.core.events import BUS, SkillCompletedEvent, SkillStartedEvent
from agent.core.ids import SkillName
from agent.core.metrics import METRICS
from agent.core.tracing import span
from agent.skills.context import SkillContext
from agent.skills.result import SkillResult


class Skill[TIn: BaseModel](ABC):
    name: ClassVar[SkillName]
    description: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    params_model: ClassVar[type[BaseModel]]
    required_caps: ClassVar[frozenset[ActionKind]] = frozenset()

    async def preconditions(self, ctx: SkillContext, params: TIn) -> None:
        return None

    @abstractmethod
    async def run(self, ctx: SkillContext, params: TIn) -> SkillResult: ...

    async def __call__(self, ctx: SkillContext, params: dict[str, object]) -> SkillResult:
        t0 = time.perf_counter()
        await BUS.publish(SkillStartedEvent(skill=self.name))
        try:
            typed = cast(TIn, self.params_model.model_validate(params))
        except ValidationError as exc:
            result = SkillResult(ok=False, skill=self.name, error=f"param validation: {exc}")
            return await self._finish(result, t0)

        try:
            await self.preconditions(ctx, typed)
        except PreconditionFailed as exc:
            result = SkillResult(
                ok=False, skill=self.name, error=str(exc), data=exc.context
            )
            return await self._finish(result, t0)

        try:
            with span(f"skill.{self.name}"):
                result = await self.run(ctx, typed)
        except AgentError as exc:
            result = SkillResult(
                ok=False, skill=self.name, error=str(exc), data=exc.to_dict()
            )
        except Exception as exc:
            result = SkillResult(ok=False, skill=self.name, error=repr(exc))

        return await self._finish(result, t0)

    async def _finish(self, result: SkillResult, t0: float) -> SkillResult:
        timed = result.with_duration((time.perf_counter() - t0) * 1000.0)
        METRICS.inc("skill.executed", skill=str(self.name), ok=str(timed.ok))
        METRICS.observe("skill.duration_ms", timed.duration_ms, skill=str(self.name))
        await BUS.publish(
            SkillCompletedEvent(
                skill=self.name, ok=timed.ok, duration_ms=timed.duration_ms
            )
        )
        return timed

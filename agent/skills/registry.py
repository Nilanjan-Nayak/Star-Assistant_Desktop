"""SkillRegistry with ``@register_skill`` decorator."""

from __future__ import annotations

import logging
from collections import defaultdict

from agent.core.errors import SkillNotFound
from agent.core.ids import SkillName
from agent.core.logging import get_logger, log_ctx
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.result import SkillResult

_log = get_logger("agent.skills")

_REGISTRY: dict[SkillName, type[Skill[object]]] = {}  # type: ignore[type-var]


def register_skill[T: type[Skill[object]]](cls: T) -> T:  # type: ignore[type-var]
    if not hasattr(cls, "name") or not hasattr(cls, "params_model"):
        raise TypeError(f"{cls.__name__} must define 'name' and 'params_model'")
    _REGISTRY[cls.name] = cls  # type: ignore[assignment]
    log_ctx(
        _log,
        logging.DEBUG,
        "skill.registered",
        name=cls.name,
        version=getattr(cls, "version", "0"),
    )
    return cls


class SkillRegistry:
    def __init__(self, ctx: SkillContext) -> None:
        self.ctx = ctx
        self._instances: dict[SkillName, Skill[object]] = {}  # type: ignore[type-var]
        self._stats: dict[SkillName, dict[str, float]] = defaultdict(
            lambda: {"runs": 0.0, "successes": 0.0, "avg_ms": 0.0}
        )

    def discover(self) -> list[SkillName]:
        for name, cls in _REGISTRY.items():
            self._instances.setdefault(name, cls())
        return list(self._instances.keys())

    def describe(self) -> list[dict[str, object]]:
        self.discover()
        return [
            {
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "params_schema": skill.params_model.model_json_schema(),
            }
            for skill in self._instances.values()
        ]

    async def run(self, name: SkillName, params: dict[str, object]) -> SkillResult:
        self.discover()
        skill = self._instances.get(name)
        if skill is None:
            raise SkillNotFound(f"unknown skill '{name}'", name=name)
        result = await skill(self.ctx, params)
        st = self._stats[name]
        n = st["runs"]
        st["runs"] = n + 1
        st["successes"] += float(result.ok)
        st["avg_ms"] = (st["avg_ms"] * n + result.duration_ms) / (n + 1)
        return result

    def stats(self) -> dict[str, dict[str, float]]:
        return {str(k): dict(v) for k, v in self._stats.items()}

    def known(self) -> frozenset[SkillName]:
        self.discover()
        return frozenset(self._instances)

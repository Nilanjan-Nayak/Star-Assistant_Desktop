from __future__ import annotations

from agent.skills.base import Skill
from agent.skills.context import DefaultSkillContext, SkillContext
from agent.skills.registry import SkillRegistry, register_skill
from agent.skills.result import SkillResult

# Trigger built-in registration
from agent.skills import builtins as _builtins  # noqa: F401

__all__ = [
    "DefaultSkillContext",
    "Skill",
    "SkillContext",
    "SkillRegistry",
    "SkillResult",
    "register_skill",
]

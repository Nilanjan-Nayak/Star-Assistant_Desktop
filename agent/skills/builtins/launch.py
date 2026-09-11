from __future__ import annotations

import os
import shutil
import subprocess

from pydantic import BaseModel, Field, StrictStr, field_validator
from typing_extensions import Annotated

from agent.core.enums import OSFamily
from agent.core.ids import parse_skill_name
from agent.geometry.monitor import CURRENT_OS
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.registry import register_skill
from agent.skills.result import SkillResult

_BLOCKED: frozenset[str] = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "bash",
        "sh",
        "zsh",
        "sudo",
        "format",
        "diskpart",
        "regedit",
        "rm",
        "dd",
    }
)


class LaunchParams(BaseModel):
    model_config = {"extra": "forbid"}
    app: Annotated[StrictStr, Field(min_length=1, max_length=100)]

    @field_validator("app")
    @classmethod
    def _not_blocked(cls, value: str) -> str:
        stem = value.strip().lower().rsplit("/", 1)[-1]
        stem = stem.rsplit("\\", 1)[-1]
        if stem in _BLOCKED or any(stem.startswith(b + ".") for b in _BLOCKED):
            raise ValueError(f"launch of {value!r} is blocked")
        if any(ch in value for ch in ";&|`$"):
            raise ValueError("shell metacharacters are not allowed in app names")
        return value


@register_skill
class LaunchSkill(Skill[LaunchParams]):
    name = parse_skill_name("launch")
    description = "Launch an application"
    params_model = LaunchParams

    async def run(self, ctx: SkillContext, params: LaunchParams) -> SkillResult:
        if ctx.governor.config.dry_run:
            return SkillResult(
                ok=True, skill=self.name, data={"app": params.app, "dry_run": True}
            )
        try:
            if CURRENT_OS is OSFamily.WINDOWS:
                os.startfile(params.app)  # type: ignore[attr-defined]
            elif CURRENT_OS is OSFamily.MACOS:
                subprocess.Popen(["open", "-a", params.app])
            else:
                binary = shutil.which(params.app)
                if binary is None:
                    return SkillResult(
                        ok=False, skill=self.name, error=f"app not on PATH: {params.app}"
                    )
                subprocess.Popen([binary])
            return SkillResult(ok=True, skill=self.name, data={"app": params.app})
        except Exception as exc:
            return SkillResult(ok=False, skill=self.name, error=str(exc))

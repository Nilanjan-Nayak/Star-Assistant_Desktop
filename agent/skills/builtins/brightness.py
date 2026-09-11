from __future__ import annotations

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from agent.core.ids import parse_skill_name
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.registry import register_skill
from agent.skills.result import SkillResult


class BrightnessParams(BaseModel):
    model_config = {"extra": "forbid"}
    level: Annotated[int, Field(ge=0, le=100)]


@register_skill
class BrightnessSkill(Skill[BrightnessParams]):
    name = parse_skill_name("brightness")
    description = "Set screen brightness (0-100)"
    params_model = BrightnessParams

    async def run(self, ctx: SkillContext, params: BrightnessParams) -> SkillResult:
        if ctx.governor.config.dry_run:
            return SkillResult(
                ok=True, skill=self.name, data={"brightness": params.level, "dry_run": True}
            )
        try:
            import screen_brightness_control as sbc

            sbc.set_brightness(params.level)
            return SkillResult(ok=True, skill=self.name, data={"brightness": params.level})
        except Exception as exc:
            return SkillResult(ok=False, skill=self.name, error=str(exc))

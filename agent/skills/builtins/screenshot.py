from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, StrictStr, field_validator
from typing_extensions import Annotated

from agent.core.ids import parse_skill_name
from agent.geometry.bbox import BoundingBox
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.registry import register_skill
from agent.skills.result import SkillResult


class ScreenshotParams(BaseModel):
    model_config = {"extra": "forbid"}
    path: Annotated[StrictStr, Field(min_length=1, max_length=260)] = "screenshot.png"
    region: BoundingBox | None = None

    @field_validator("path")
    @classmethod
    def _no_parent_escape(cls, value: str) -> str:
        # Keep the write inside the working directory; reject absolute / `..`.
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("screenshot path must be a relative path without '..'")
        if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("screenshot path must end in .png/.jpg/.jpeg/.webp")
        return value


@register_skill
class ScreenshotSkill(Skill[ScreenshotParams]):
    name = parse_skill_name("screenshot")
    description = "Save a screenshot to disk"
    params_model = ScreenshotParams

    async def run(self, ctx: SkillContext, params: ScreenshotParams) -> SkillResult:
        if ctx.governor.config.dry_run:
            return SkillResult(
                ok=True, skill=self.name, data={"path": params.path, "dry_run": True}
            )
        try:
            snap = ctx.world.capture.grab(region=params.region)
            image = snap.raster.to_pil()
            save = getattr(image, "save", None)
            if save is None:
                raise RuntimeError("raster could not be converted to a saveable image")
            save(params.path)
            return SkillResult(
                ok=True,
                skill=self.name,
                data={"path": params.path, "size": [snap.width, snap.height]},
            )
        except Exception as exc:
            return SkillResult(ok=False, skill=self.name, error=str(exc))

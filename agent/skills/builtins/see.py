"""Look at the screen: capture + OCR. Read-only — runs even in dry-run."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, StrictStr, field_validator
from typing_extensions import Annotated

from agent.core.ids import parse_skill_name
from agent.geometry.bbox import BoundingBox
from agent.perception.ocr_engine import engine_name, group_lines
from agent.perception.ocr_engine import OcrToken
from agent.perception.strategies.ocr import OCRPerception
from agent.skills.base import Skill
from agent.skills.context import SkillContext
from agent.skills.registry import register_skill
from agent.skills.result import SkillResult

_DUMP = "*"


class SeeParams(BaseModel):
    model_config = {"extra": "forbid"}
    query: Annotated[StrictStr, Field(min_length=1, max_length=500)] = _DUMP
    save_path: Annotated[StrictStr, Field(min_length=1, max_length=260)] | None = "see.png"
    region: BoundingBox | None = None
    min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.3

    @field_validator("save_path")
    @classmethod
    def _relative_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = Path(value)
        if ".." in candidate.parts:
            raise ValueError("save_path must not contain '..'")
        if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("save_path must end in .png/.jpg/.jpeg/.webp")
        return value


@register_skill
class SeeSkill(Skill[SeeParams]):
    name = parse_skill_name("see")
    description = "Capture the screen and read visible text (OCR)"
    params_model = SeeParams

    async def run(self, ctx: SkillContext, params: SeeParams) -> SkillResult:
        try:
            snap = ctx.world.observe() if params.region is None else ctx.world.capture.grab(
                region=params.region
            )
        except Exception as exc:
            return SkillResult(ok=False, skill=self.name, error=f"capture failed: {exc}")

        saved: str | None = None
        if params.save_path is not None:
            try:
                snap.raster.save(params.save_path)
                saved = params.save_path
            except Exception as exc:
                return SkillResult(
                    ok=False, skill=self.name, error=f"could not save screenshot: {exc}"
                )

        words = await ctx.perception.read_all(snap.raster, params.region)
        words = [w for w in words if w.confidence >= params.min_confidence]
        if params.query.strip() != _DUMP:
            needle = params.query.lower()
            words = [w for w in words if needle in w.text.lower()]

        engine = "unknown"
        for strat in ctx.perception.strategies:
            if isinstance(strat, OCRPerception):
                engine = engine_name(strat.engine)
                break

        tokens = [
            OcrToken(
                text=w.text,
                x=w.bbox.x,
                y=w.bbox.y,
                width=w.bbox.width,
                height=w.bbox.height,
                confidence=w.confidence,
            )
            for w in words
        ]
        lines = group_lines(tokens)
        note = None
        if not words and engine in {"empty", "unknown"}:
            note = (
                "no OCR engine installed — screenshot saved if requested. "
                "Install tesseract + pytesseract (or easyocr) to read text."
            )

        return SkillResult(
            ok=True,
            skill=self.name,
            data={
                "text": "\n".join(lines),
                "lines": lines,
                "elements": [
                    {
                        "text": w.text,
                        "x": w.bbox.x,
                        "y": w.bbox.y,
                        "width": w.bbox.width,
                        "height": w.bbox.height,
                        "confidence": w.confidence,
                    }
                    for w in words
                ],
                "size": [snap.width, snap.height],
                "engine": engine,
                "path": saved,
                "note": note,
            },
        )

"""Vision-LLM fallback strategy. Stubbed so the package imports without Anthropic."""

from __future__ import annotations

from typing import ClassVar

from agent.core.enums import PerceptionSource
from agent.geometry.raster import Raster
from agent.perception.element import ScreenElement
from agent.perception.query import PerceptionQuery
from agent.perception.strategy import StrategyMixin


class VisionLLMPerception(StrategyMixin):
    name: ClassVar[PerceptionSource] = PerceptionSource.VISION_LLM

    def __init__(self, model: str = "claude-sonnet-4-20250514") -> None:
        self.model = model

    async def find(self, screenshot: Raster, query: PerceptionQuery) -> list[ScreenElement]:
        _ = (screenshot, query, self.model)
        return []

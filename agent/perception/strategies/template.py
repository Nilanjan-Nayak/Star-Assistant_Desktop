"""Template-matching strategy (icons / logos). Stubbed without OpenCV."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agent.core.enums import PerceptionSource
from agent.geometry.raster import Raster
from agent.perception.element import ScreenElement
from agent.perception.query import PerceptionQuery
from agent.perception.strategy import StrategyMixin


class TemplatePerception(StrategyMixin):
    name: ClassVar[PerceptionSource] = PerceptionSource.TEMPLATE

    def __init__(self, template_dir: Path = Path("./templates")) -> None:
        self.template_dir = template_dir

    async def find(self, screenshot: Raster, query: PerceptionQuery) -> list[ScreenElement]:
        _ = (screenshot, query, self.template_dir)
        return []

"""OS accessibility-tree strategy.

Real implementations are OS-specific and pulled in via extras:

* Windows — ``pip install uiautomation``
* macOS   — ``pip install pyobjc-framework-ApplicationServices``
* Linux   — ``pip install pyatspi``

This module stays importable everywhere; it returns ``[]`` when the native
backend is absent so the cascade can fall through to OCR.
"""

from __future__ import annotations

from typing import ClassVar

from agent.core.enums import PerceptionSource
from agent.geometry.raster import Raster
from agent.perception.element import ScreenElement
from agent.perception.query import PerceptionQuery
from agent.perception.strategy import StrategyMixin


class A11yPerception(StrategyMixin):
    name: ClassVar[PerceptionSource] = PerceptionSource.A11Y

    async def find(self, screenshot: Raster, query: PerceptionQuery) -> list[ScreenElement]:
        _ = (screenshot, query)
        return []

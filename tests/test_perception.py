from __future__ import annotations

import pytest

from agent.core.enums import PerceptionSource
from agent.core.errors import PerceptionError
from agent.geometry.bbox import BoundingBox
from agent.geometry.raster import solid
from agent.perception.cascade import PerceptionCascade
from agent.perception.element import ScreenElement
from agent.perception.query import PerceptionQuery
from agent.perception.strategy import StrategyMixin


class _HitStrategy(StrategyMixin):
    name = PerceptionSource.OCR

    async def find(self, screenshot: object, query: PerceptionQuery) -> list[ScreenElement]:
        _ = screenshot
        return [
            ScreenElement(
                text=query.text,
                bbox=BoundingBox(x=1, y=1, width=8, height=8),
                source=PerceptionSource.OCR,
                confidence=0.9,
            )
        ]


class _EmptyStrategy(StrategyMixin):
    name = PerceptionSource.A11Y

    async def find(self, screenshot: object, query: PerceptionQuery) -> list[ScreenElement]:
        _ = (screenshot, query)
        return []


@pytest.mark.asyncio
async def test_cascade_falls_through() -> None:
    cascade = PerceptionCascade(strategies=[_EmptyStrategy(), _HitStrategy()])
    found = await cascade.find(solid(16, 16), PerceptionQuery(text="OK"))
    assert found[0].text == "OK"


@pytest.mark.asyncio
async def test_cascade_exhausted() -> None:
    cascade = PerceptionCascade(strategies=[_EmptyStrategy()])
    with pytest.raises(PerceptionError):
        await cascade.find(solid(16, 16), PerceptionQuery(text="missing"))


def test_fuzzy_match() -> None:
    mixin = StrategyMixin()
    assert mixin.fuzzy_match("OK", "OK") == 1.0
    assert mixin.fuzzy_match("ok", "okay button") >= 0.9
    assert mixin.fuzzy_match("", "x") == 0.0

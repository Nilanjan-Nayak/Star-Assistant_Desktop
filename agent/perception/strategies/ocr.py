"""OCR perception strategy — engine-agnostic (Tesseract / EasyOCR / scripted)."""

from __future__ import annotations

from typing import ClassVar

from agent.core.enums import ElementRole, PerceptionSource
from agent.geometry.bbox import BoundingBox
from agent.geometry.raster import Raster
from agent.perception.element import ScreenElement
from agent.perception.ocr_engine import OcrEngine, OcrToken, detect_engine
from agent.perception.query import PerceptionQuery
from agent.perception.strategy import StrategyMixin

DUMP_QUERY = "*"


def tokens_to_elements(
    tokens: list[OcrToken],
    *,
    role: ElementRole = ElementRole.TEXT,
) -> list[ScreenElement]:
    elements: list[ScreenElement] = []
    for tok in tokens:
        try:
            bbox = BoundingBox(x=tok.x, y=tok.y, width=tok.width, height=tok.height)
        except Exception:
            continue
        elements.append(
            ScreenElement(
                text=tok.text,
                bbox=bbox,
                role=role,
                confidence=tok.confidence,
                source=PerceptionSource.OCR,
            )
        )
    return elements


class OCRPerception(StrategyMixin):
    name: ClassVar[PerceptionSource] = PerceptionSource.OCR

    def __init__(self, engine: OcrEngine | None = None) -> None:
        self.engine = engine if engine is not None else detect_engine()

    async def extract(
        self,
        screenshot: Raster,
        region: BoundingBox | None = None,
    ) -> list[ScreenElement]:
        tokens = self.engine.read(screenshot, region)
        elements = tokens_to_elements(tokens)
        elements.sort(key=lambda el: (el.bbox.y, el.bbox.x))
        return elements

    async def find(self, screenshot: Raster, query: PerceptionQuery) -> list[ScreenElement]:
        elements = await self.extract(screenshot, query.region)
        if query.text.strip() == DUMP_QUERY:
            return elements[: query.max_results]
        results: list[ScreenElement] = []
        for el in elements:
            score = (
                self.fuzzy_match(query.text, el.text)
                if query.fuzzy
                else (1.0 if query.text.lower() in el.text.lower() else 0.0)
            )
            combined = max(0.0, min(1.0, score * el.confidence))
            if combined < query.min_confidence:
                continue
            results.append(el.model_copy(update={"confidence": combined}))
        results.sort(key=lambda item: item.confidence, reverse=True)
        return results[: query.max_results]

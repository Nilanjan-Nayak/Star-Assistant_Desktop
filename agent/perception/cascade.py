"""PerceptionCascade — runs strategies in priority order until one hits."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from agent.core.errors import PerceptionError
from agent.core.ids import ScreenHash
from agent.core.logging import get_logger, log_ctx
from agent.core.metrics import METRICS
from agent.core.tracing import span
from agent.geometry.bbox import BoundingBox
from agent.geometry.raster import Raster
from agent.perception.cache import ElementLocatorCache
from agent.perception.element import ScreenElement
from agent.perception.query import PerceptionQuery
from agent.perception.strategies import (
    A11yPerception,
    OCRPerception,
    TemplatePerception,
    VisionLLMPerception,
)
from agent.perception.strategy import PerceptionStrategy

_log = get_logger("agent.perception")


def _dedupe(elements: list[ScreenElement]) -> list[ScreenElement]:
    unique: list[ScreenElement] = []
    for el in elements:
        if any(el.is_duplicate_of(existing) for existing in unique):
            continue
        unique.append(el)
    return unique


class PerceptionCascade:
    def __init__(
        self,
        strategies: Sequence[PerceptionStrategy] | None = None,
        cache: ElementLocatorCache | None = None,
    ) -> None:
        self.strategies: list[PerceptionStrategy] = (
            list(strategies)
            if strategies is not None
            else [
                A11yPerception(),
                OCRPerception(),
                TemplatePerception(),
                VisionLLMPerception(),
            ]
        )
        self.cache = cache or ElementLocatorCache()

    async def find(
        self,
        screenshot: Raster,
        query: PerceptionQuery,
        screen_hash: ScreenHash | None = None,
    ) -> list[ScreenElement]:
        if screen_hash is not None:
            hit = self.cache.get(query.text, screen_hash)
            if hit is not None:
                return [hit]

        last_error: PerceptionError | None = None
        for strat in self.strategies:
            with span(f"perception.{strat.name.value}"):
                try:
                    results = await strat.find(screenshot, query)
                except PerceptionError as exc:
                    last_error = exc
                    METRICS.inc("perception.error", strategy=strat.name.value)
                    continue
                except Exception as exc:
                    log_ctx(
                        _log,
                        logging.WARNING,
                        "perception.unexpected",
                        strategy=strat.name.value,
                        error=str(exc),
                    )
                    METRICS.inc("perception.error", strategy=strat.name.value)
                    continue

            filtered = [r for r in results if r.confidence >= query.min_confidence]
            if query.dedupe:
                filtered = _dedupe(filtered)
            METRICS.observe("perception.results", float(len(filtered)), strategy=strat.name.value)
            if filtered:
                METRICS.inc("perception.success", strategy=strat.name.value)
                if screen_hash is not None:
                    self.cache.put(query.text, screen_hash, filtered[0])
                return filtered[: query.max_results]

        METRICS.inc("perception.exhausted")
        detail = last_error.to_dict() if last_error is not None else {}
        raise PerceptionError(
            f"no strategy found {query.text!r}",
            query=query.model_dump(mode="json"),
            last_error=detail,
        )

    async def read_all(
        self,
        screenshot: Raster,
        region: BoundingBox | None = None,
    ) -> list[ScreenElement]:
        """Dump every word the first capable strategy can see."""
        for strat in self.strategies:
            extract = getattr(strat, "extract", None)
            if extract is None:
                continue
            try:
                words = await extract(screenshot, region)
            except Exception as exc:
                log_ctx(
                    _log,
                    logging.WARNING,
                    "perception.extract.failed",
                    strategy=getattr(getattr(strat, "name", None), "value", "?"),
                    error=str(exc),
                )
                continue
            if words:
                return list(words)
        return []

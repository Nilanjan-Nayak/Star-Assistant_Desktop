"""``PerceptionStrategy`` protocol and shared fuzzy-matching helpers."""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from agent.core.enums import PerceptionSource
from agent.geometry.raster import Raster
from agent.perception.element import ScreenElement
from agent.perception.query import PerceptionQuery


@runtime_checkable
class PerceptionStrategy(Protocol):
    """Every perception source implements this."""

    name: ClassVar[PerceptionSource]

    async def find(self, screenshot: Raster, query: PerceptionQuery) -> list[ScreenElement]: ...


class StrategyMixin:
    """Fuzzy-matching helpers shared across strategies."""

    @staticmethod
    def fuzzy_match(needle: str, hay: str) -> float:
        n, h = needle.lower().strip(), hay.lower().strip()
        if not n or not h:
            return 0.0
        if n == h:
            return 1.0
        if n in h:
            return 0.9 + 0.1 * (len(n) / max(len(h), 1))
        # Dice-style character overlap, penalised so it never outranks containment.
        n_set, h_set = set(n), set(h)
        overlap = len(n_set & h_set)
        return (overlap / max(len(n_set), 1)) * 0.55

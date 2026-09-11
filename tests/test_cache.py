from __future__ import annotations

from agent.core.clock import FrozenClock
from agent.core.enums import ElementRole, PerceptionSource
from agent.core.ids import ScreenHash
from agent.geometry.bbox import BoundingBox
from agent.perception.cache import ElementLocatorCache
from agent.perception.element import ScreenElement


def _el() -> ScreenElement:
    return ScreenElement(
        text="OK",
        bbox=BoundingBox(x=0, y=0, width=10, height=10),
        role=ElementRole.BUTTON,
        source=PerceptionSource.OCR,
    )


def test_ttl_expiry() -> None:
    clock = FrozenClock()
    cache = ElementLocatorCache(ttl_seconds=5.0, clock=clock)
    digest = ScreenHash("a" * 64)
    cache.put("ok", digest, _el())
    assert cache.get("ok", digest) is not None
    clock.advance(6.0)
    assert cache.get("ok", digest) is None


def test_lru_eviction() -> None:
    clock = FrozenClock()
    cache = ElementLocatorCache(ttl_seconds=100.0, max_entries=1, clock=clock)
    a = ScreenHash("a" * 64)
    b = ScreenHash("b" * 64)
    cache.put("one", a, _el())
    cache.put("two", b, _el())
    assert cache.get("one", a) is None
    assert cache.get("two", b) is not None

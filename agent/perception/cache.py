"""TTL / LRU element locator cache, keyed by (query text, screen hash)."""

from __future__ import annotations

from dataclasses import dataclass

from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.ids import ScreenHash
from agent.core.metrics import METRICS
from agent.perception.element import ScreenElement


@dataclass(slots=True)
class _CacheEntry:
    element: ScreenElement
    inserted_at: float


class ElementLocatorCache:
    def __init__(
        self,
        ttl_seconds: float = 300.0,
        max_entries: int = 1000,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._store: dict[str, _CacheEntry] = {}

    @staticmethod
    def _key(query_text: str, screen_hash: ScreenHash) -> str:
        return f"{query_text.lower().strip()}|{screen_hash[:16]}"

    def get(self, query_text: str, screen_hash: ScreenHash) -> ScreenElement | None:
        key = self._key(query_text, screen_hash)
        entry = self._store.get(key)
        if entry is None:
            METRICS.inc("perception.cache.miss")
            return None
        if self.clock.monotonic() - entry.inserted_at > self.ttl:
            self._store.pop(key, None)
            METRICS.inc("perception.cache.expired")
            return None
        METRICS.inc("perception.cache.hit")
        return entry.element

    def put(self, query_text: str, screen_hash: ScreenHash, element: ScreenElement) -> None:
        if len(self._store) >= self.max_entries:
            oldest_key = min(self._store.items(), key=lambda kv: kv[1].inserted_at)[0]
            self._store.pop(oldest_key, None)
        self._store[self._key(query_text, screen_hash)] = _CacheEntry(
            element=element,
            inserted_at=self.clock.monotonic(),
        )

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

"""Clock abstraction — makes time, rate-limits, TTLs and breakers testable."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Wall-clock UTC timestamp."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, suitable for durations."""
        ...


class SystemClock:
    """Production clock backed by ``datetime.now(UTC)`` + ``perf_counter``."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.perf_counter()


class FrozenClock:
    """Deterministic clock. Time only advances when you call :meth:`advance`."""

    def __init__(self, start: datetime | None = None, *, mono: float = 0.0) -> None:
        self._now: datetime = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._mono: float = mono

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot rewind FrozenClock")
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, when: datetime, *, mono: float | None = None) -> None:
        if when.tzinfo is None:
            raise ValueError("FrozenClock requires timezone-aware datetimes")
        self._now = when
        if mono is not None:
            self._mono = mono


DEFAULT_CLOCK: Clock = SystemClock()

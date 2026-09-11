"""3-state circuit breaker (closed → open → half-open) with a single trial."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.enums import BreakerState
from agent.core.errors import CircuitOpen
from agent.core.ids import BreakerName


@dataclass(slots=True)
class CircuitBreaker:
    """Failure-counting breaker.

    * **closed** — calls flow; failures increment a counter.
    * **open** — ``guard()`` raises ``CircuitOpen`` until ``reset_after`` seconds.
    * **half-open** — exactly one trial is admitted; success closes, failure re-opens.
    """

    name: BreakerName
    threshold: int = 5
    reset_after: float = 30.0
    clock: Clock = field(default_factory=lambda: DEFAULT_CLOCK)
    _failures: int = 0
    _opened_at: float | None = None
    _half_open_inflight: bool = False

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if self.reset_after <= 0:
            raise ValueError("reset_after must be > 0")

    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self.clock.monotonic() - self._opened_at > self.reset_after:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def guard(self) -> None:
        current = self.state()
        if current is BreakerState.OPEN:
            raise CircuitOpen(
                f"circuit '{self.name}' is open",
                breaker=self.name,
                failures=self._failures,
            )
        if current is BreakerState.HALF_OPEN:
            if self._half_open_inflight:
                raise CircuitOpen(
                    f"circuit '{self.name}' half-open trial already in flight",
                    breaker=self.name,
                )
            self._half_open_inflight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open_inflight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._half_open_inflight = False
        if self._failures >= self.threshold or self._opened_at is not None:
            self._opened_at = self.clock.monotonic()

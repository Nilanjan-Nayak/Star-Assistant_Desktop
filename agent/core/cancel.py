"""Cooperative cancellation token — checked at await boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.errors import CancelledError, TimeoutExceeded


@dataclass(slots=True)
class CancellationToken:
    """Flag + optional deadline. ``check()`` raises if cancelled or expired."""

    clock: Clock = field(default_factory=lambda: DEFAULT_CLOCK)
    _cancelled: bool = False
    _deadline: float | None = None
    _reason: str = "cancelled"

    @classmethod
    def timeout(cls, seconds: float, *, clock: Clock = DEFAULT_CLOCK) -> CancellationToken:
        token = cls(clock=clock)
        token._deadline = clock.monotonic() + seconds
        token._reason = f"timeout after {seconds:.3f}s"
        return token

    def cancel(self, reason: str = "cancelled") -> None:
        self._cancelled = True
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._deadline is not None and self.clock.monotonic() >= self._deadline:
            return True
        return False

    def check(self) -> None:
        if self._cancelled:
            raise CancelledError(self._reason)
        if self._deadline is not None and self.clock.monotonic() >= self._deadline:
            raise TimeoutExceeded(self._reason, deadline=self._deadline)

    def remaining(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self.clock.monotonic())

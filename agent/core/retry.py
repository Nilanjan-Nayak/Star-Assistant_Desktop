"""First-class retry policy — jittered exponential backoff with a budget."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent.core.cancel import CancellationToken
from agent.core.errors import AgentError, TimeoutExceeded


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Immutable retry configuration.

    ``attempts`` is the *total* number of tries (1 = no retry).
    Delay before try *n* (1-indexed, n>=2) is::

        min(max_backoff, base_backoff * factor ** (n-2)) * jitter
    """

    attempts: int = 3
    base_backoff: float = 0.25
    max_backoff: float = 8.0
    factor: float = 2.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")
        if self.base_backoff < 0 or self.max_backoff < 0:
            raise ValueError("backoff must be >= 0")
        if self.factor < 1.0:
            raise ValueError("factor must be >= 1")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be in [0, 1]")

    def delay_for(self, attempt: int) -> float:
        """Seconds to sleep *after* a failed ``attempt`` (1-indexed) before retrying."""
        if attempt < 1:
            raise ValueError("attempt is 1-indexed")
        raw = min(self.max_backoff, self.base_backoff * (self.factor ** (attempt - 1)))
        if self.jitter == 0.0:
            return raw
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))

    async def run[T](
        self,
        op: Callable[[int], Awaitable[T]],
        *,
        retry_on: tuple[type[BaseException], ...] = (AgentError,),
        token: CancellationToken | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> T:
        """Execute ``op(attempt)`` until it returns or the budget is spent."""
        import asyncio

        sleep = sleeper or asyncio.sleep
        last: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            if token is not None:
                token.check()
            try:
                return await op(attempt)
            except retry_on as exc:
                last = exc
                if attempt >= self.attempts:
                    break
                await sleep(self.delay_for(attempt))
        if last is None:
            raise TimeoutExceeded("retry policy exhausted with no exception")
        raise last


DEFAULT_RETRY: RetryPolicy = RetryPolicy()
NO_RETRY: RetryPolicy = RetryPolicy(attempts=1, base_backoff=0.0, jitter=0.0)

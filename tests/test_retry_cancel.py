from __future__ import annotations

import pytest

from agent.core.cancel import CancellationToken
from agent.core.clock import FrozenClock
from agent.core.errors import CancelledError, MotorError, TimeoutExceeded
from agent.core.retry import RetryPolicy


def test_timeout_token() -> None:
    clock = FrozenClock()
    token = CancellationToken.timeout(1.0, clock=clock)
    token.check()
    clock.advance(1.5)
    with pytest.raises(TimeoutExceeded):
        token.check()


def test_cancel_token() -> None:
    token = CancellationToken()
    token.cancel("stop")
    with pytest.raises(CancelledError):
        token.check()


@pytest.mark.asyncio
async def test_retry_eventually_succeeds() -> None:
    policy = RetryPolicy(attempts=3, base_backoff=0.0, jitter=0.0)
    calls = {"n": 0}

    async def flaky(attempt: int) -> int:
        calls["n"] += 1
        if attempt < 3:
            raise MotorError("nope")
        return 7

    async def no_sleep(_: float) -> None:
        return None

    value = await policy.run(flaky, sleeper=no_sleep)
    assert value == 7
    assert calls["n"] == 3

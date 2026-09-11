from __future__ import annotations

import pytest

from agent.core.circuit_breaker import CircuitBreaker
from agent.core.clock import FrozenClock
from agent.core.enums import BreakerState
from agent.core.errors import CircuitOpen
from agent.core.ids import BreakerName


def test_opens_after_threshold() -> None:
    clock = FrozenClock()
    br = CircuitBreaker(name=BreakerName("t"), threshold=2, reset_after=10.0, clock=clock)
    br.record_failure()
    assert br.state() is BreakerState.CLOSED
    br.record_failure()
    assert br.state() is BreakerState.OPEN
    with pytest.raises(CircuitOpen):
        br.guard()


def test_half_open_single_trial() -> None:
    clock = FrozenClock()
    br = CircuitBreaker(name=BreakerName("t"), threshold=1, reset_after=5.0, clock=clock)
    br.record_failure()
    clock.advance(6.0)
    assert br.state() is BreakerState.HALF_OPEN
    br.guard()
    with pytest.raises(CircuitOpen):
        br.guard()
    br.record_success()
    assert br.state() is BreakerState.CLOSED

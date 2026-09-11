"""MotorController — retry, verify, circuit-break, emit events."""

from __future__ import annotations

import asyncio
import logging

from agent.core.cancel import CancellationToken
from agent.core.circuit_breaker import CircuitBreaker
from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.enums import NO_SCREEN_VERIFY_ACTIONS, ActionKind
from agent.core.errors import CircuitOpen, MotorError, TimeoutExceeded
from agent.core.events import BUS, ActionCompletedEvent, ActionStartedEvent
from agent.core.ids import BreakerName
from agent.core.logging import get_logger, log_ctx
from agent.core.metrics import METRICS
from agent.core.retry import DEFAULT_RETRY, RetryPolicy
from agent.core.tracing import span
from agent.motor.backends.base import MotorBackend
from agent.motor.result import ActionResult
from agent.motor.spec import ActionSpec
from agent.world.model import WorldModel

_log = get_logger("agent.motor")


class MotorController:
    def __init__(
        self,
        backend: MotorBackend,
        world: WorldModel,
        retry: RetryPolicy | None = None,
        verify_delay: float = 0.5,
        clock: Clock = DEFAULT_CLOCK,
        change_threshold: float = 0.02,
    ) -> None:
        self.backend = backend
        self.world = world
        self.retry = retry or DEFAULT_RETRY
        self.verify_delay = verify_delay
        self.clock = clock
        self.change_threshold = change_threshold
        self._breakers: dict[ActionKind, CircuitBreaker] = {
            kind: CircuitBreaker(name=BreakerName(f"motor.{kind.value}"), clock=clock)
            for kind in ActionKind
        }

    async def execute(
        self,
        spec: ActionSpec,
        *,
        token: CancellationToken | None = None,
    ) -> ActionResult:
        breaker = self._breakers[spec.kind]
        try:
            breaker.guard()
        except CircuitOpen as exc:
            return ActionResult(
                action_id=spec.action_id,
                kind=spec.kind,
                success=False,
                detail=f"circuit open: {exc}",
            )

        await BUS.publish(
            ActionStartedEvent(action_id=spec.action_id, action_kind=spec.kind)
        )

        last: ActionResult | None = None
        for attempt in range(1, self.retry.attempts + 1):
            if token is not None:
                token.check()
            try:
                with span(f"motor.{spec.kind.value}", attempt=attempt):
                    result = await asyncio.wait_for(
                        self._execute_once(spec, attempt),
                        timeout=spec.timeout,
                    )
            except TimeoutError as exc:
                breaker.record_failure()
                METRICS.inc("motor.timeout", kind=spec.kind.value)
                last = ActionResult(
                    action_id=spec.action_id,
                    kind=spec.kind,
                    success=False,
                    detail=f"timeout: {exc}",
                    retries=attempt,
                )
            except MotorError as exc:
                breaker.record_failure()
                METRICS.inc("motor.error", kind=spec.kind.value)
                last = ActionResult(
                    action_id=spec.action_id,
                    kind=spec.kind,
                    success=False,
                    detail=str(exc),
                    retries=attempt,
                )
            else:
                needs_change = spec.verify and spec.kind not in NO_SCREEN_VERIFY_ACTIONS
                if result.success and (not needs_change or result.screen_changed):
                    breaker.record_success()
                    METRICS.inc("motor.success", kind=spec.kind.value)
                    METRICS.observe(
                        "motor.duration_ms", result.duration_ms, kind=spec.kind.value
                    )
                    await BUS.publish(
                        ActionCompletedEvent(
                            action_id=spec.action_id,
                            success=True,
                            duration_ms=result.duration_ms,
                        )
                    )
                    return result
                last = result
                log_ctx(
                    _log,
                    logging.WARNING,
                    "motor.retry",
                    action=spec.kind.value,
                    attempt=attempt,
                    screen_changed=result.screen_changed,
                )

            if attempt < self.retry.attempts:
                await asyncio.sleep(self.retry.delay_for(attempt))

        breaker.record_failure()
        final = last or ActionResult(
            action_id=spec.action_id,
            kind=spec.kind,
            success=False,
            detail="retries exhausted",
            retries=self.retry.attempts,
        )
        await BUS.publish(
            ActionCompletedEvent(
                action_id=spec.action_id,
                success=False,
                duration_ms=final.duration_ms,
            )
        )
        return final

    async def _execute_once(self, spec: ActionSpec, attempt: int) -> ActionResult:
        t0 = self.clock.monotonic()
        before = self.world.current if spec.verify else None

        await self.backend.execute(spec)

        screen_changed = False
        diff_ratio = 0.0
        if spec.verify and before is not None and spec.kind not in NO_SCREEN_VERIFY_ACTIONS:
            await asyncio.sleep(self.verify_delay)
            after = self.world.observe()
            diff_ratio = self.world.differ.diff_ratio(before, after)
            screen_changed = diff_ratio > self.change_threshold

        return ActionResult(
            action_id=spec.action_id,
            kind=spec.kind,
            success=True,
            screen_changed=screen_changed,
            diff_ratio=diff_ratio,
            duration_ms=(self.clock.monotonic() - t0) * 1000.0,
            retries=attempt - 1,
        )


# Silence unused import if TimeoutExceeded is only documented.
_ = TimeoutExceeded

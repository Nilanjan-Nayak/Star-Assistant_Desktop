"""Honest recovery and bounded retry logic for Phase 10 orchestration.

Blueprint §10 non-negotiables:
* OBSERVE → ACT → VERIFY → RECOVER
* Never a blind infinite retry loop (bounded retries with honesty)
* Actions blocked by policy/safety are NEVER retried — they transition to BLOCKED
* Successful recovery marks the task state as RECOVERED (a terminal success variant)
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from Backend.star.brain.schemas import Task, TaskState
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

_log = star_logger("orchestrator.recovery")

_NON_RETRYABLE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.BLOCKED,
        TaskState.WAITING_CONFIRMATION,
        TaskState.CANCELLED,
        TaskState.DONE,
        TaskState.RECOVERED,
    }
)


class RecoveryManager:
    """Manages retry budgets and honest recovery decisions for plan tasks."""

    def __init__(self, settings: Settings, *, bus: StarEventBus | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.stats = {"retried": 0, "recovered": 0, "abandoned": 0, "blocked": 0}

    def can_retry(self, task: Task) -> bool:
        """Whether this task is eligible for another attempt."""
        if task.state in _NON_RETRYABLE_STATES:
            return False
        max_allowed = min(task.max_retries, self.settings.orchestrator.max_retries)
        return task.retries < max_allowed

    def is_safety_refusal(self, task: Task, error: str = "") -> bool:
        """Detect whether a failure was caused by policy/security rejection."""
        if task.state is TaskState.BLOCKED:
            return True
        err = (error or task.error or "").lower()
        safety_markers = (
            "emergency stop",
            "policy refusal",
            "permission denied",
            "denied by policy",
            "blocked by policy",
            "guardrail",
            "security violation",
        )
        return any(marker in err for marker in safety_markers)

    async def attempt_recovery(
        self,
        task: Task,
        execute_fn: Callable[[], Awaitable[bool]],
        *,
        session_id: str = "",
    ) -> bool:
        """Attempt honest recovery via bounded retry.

        Returns True if the task successfully recovered, False otherwise.
        """
        if not self.can_retry(task):
            if self.is_safety_refusal(task):
                task.state = TaskState.BLOCKED
                self.stats["blocked"] += 1
            else:
                task.state = TaskState.FAILED
                self.stats["abandoned"] += 1
            return False

        while self.can_retry(task):
            task.retries += 1
            self.stats["retried"] += 1
            _log.info(
                "star2.orchestrator.recovery.retry task_id=%s attempt=%d/%d goal=%r",
                task.task_id, task.retries, task.max_retries, task.goal[:80],
            )
            # Short exponential backoff (bounded)
            await asyncio.sleep(min(0.2 * (2 ** (task.retries - 1)), 1.5))

            try:
                ok = await execute_fn()
                if ok:
                    task.state = TaskState.RECOVERED
                    self.stats["recovered"] += 1
                    if self.bus is not None:
                        self.bus.emit(
                            "task.recovered",
                            phase=EventPhase.TASK,
                            session_id=session_id,
                            task_id=task.task_id,
                            goal=task.goal,
                            agent=task.agent.value,
                            attempts=task.retries + 1,
                        )
                    _log.info("star2.orchestrator.recovery.succeeded task_id=%s", task.task_id)
                    return True
            except Exception as exc:  # noqa: BLE001
                task.error = str(exc)
                _log.warning("star2.orchestrator.recovery.attempt_failed task_id=%s error=%s", task.task_id, exc)

        # Retries exhausted
        task.state = TaskState.FAILED
        self.stats["abandoned"] += 1
        if self.bus is not None:
            self.bus.emit(
                "task.failed",
                phase=EventPhase.TASK,
                session_id=session_id,
                task_id=task.task_id,
                goal=task.goal,
                agent=task.agent.value,
                error=task.error or "all recovery retries exhausted",
            )
        return False

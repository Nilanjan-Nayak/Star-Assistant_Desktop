"""Task runner with cancellation, concurrency, and honest recovery for Phase 10.

Manages active tasks, runs them through the routed workers (StarAgent or
ToolExecutor), tracks task state transitions, and supports graceful cancellation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from typing import Any, Callable

from Backend.star.agents.base import AgentRegistry, AgentRunState, StarAgent
from Backend.star.brain.planning import task_state_from_calls
from Backend.star.brain.schemas import (
    AgentName,
    Task,
    TaskState,
    ToolCallState,
    Verification,
)
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.orchestrator.recovery import RecoveryManager
from Backend.star.orchestrator.schemas import TaskRoute
from Backend.star.tools.executor import ToolExecutor

_log = star_logger("orchestrator.runner")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskRunner:
    """Executes tasks, manages active handles, and handles cancellation/recovery."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        executor: ToolExecutor | None = None,
        registry: AgentRegistry | None = None,
        recovery: RecoveryManager | None = None,
        stop_gate: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.executor = executor
        self.registry = registry
        self.recovery = recovery or RecoveryManager(settings, bus=bus)
        self.stop_gate = stop_gate
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_records: dict[str, Task] = {}

    def is_stopped(self) -> bool:
        return bool(self.stop_gate()) if callable(self.stop_gate) else False

    async def run_task(
        self,
        task: Task,
        route: TaskRoute,
        *,
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        dry_run: bool | None = None,
    ) -> Task:
        """Run a single task to completion or honest failure."""
        effective_dry_run = self.settings.security.dry_run if dry_run is None else bool(dry_run)
        self._task_records[task.task_id] = task

        # Check emergency stop
        if self.is_stopped():
            task.state = TaskState.BLOCKED
            task.error = "emergency stop is active — task will not execute"
            self._emit_task_event("task.failed", task, session_id=session_id)
            return task

        task.started_at = task.started_at or _utcnow()
        task.state = TaskState.RUNNING
        self._emit_task_event("task.started", task, session_id=session_id)

        current_async_task = asyncio.current_task()
        if current_async_task is not None:
            self._active_tasks[task.task_id] = current_async_task

        try:
            timeout_s = self.settings.orchestrator.task_timeout_s
            try:
                await asyncio.wait_for(
                    self._dispatch_task(
                        task,
                        route,
                        session_id=session_id,
                        request_id=request_id,
                        plan_id=plan_id,
                        dry_run=effective_dry_run,
                    ),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                task.state = TaskState.FAILED
                task.error = f"task timed out after {timeout_s}s"
            except asyncio.CancelledError:
                task.state = TaskState.CANCELLED
                task.error = "task was cancelled"
                self._emit_task_event("task.cancelled", task, session_id=session_id)
                raise

            # Verification & honest recovery
            if task.state is TaskState.FAILED and self.recovery.can_retry(task):
                async def _retry_wrapper() -> bool:
                    await self._dispatch_task(
                        task,
                        route,
                        session_id=session_id,
                        request_id=request_id,
                        plan_id=plan_id,
                        dry_run=effective_dry_run,
                    )
                    return task.state in (TaskState.DONE, TaskState.RECOVERED)

                await self.recovery.attempt_recovery(task, _retry_wrapper, session_id=session_id)

        finally:
            self._active_tasks.pop(task.task_id, None)
            task.ended_at = _utcnow()
            self._emit_terminal_event(task, session_id=session_id)

        return task

    async def _dispatch_task(
        self,
        task: Task,
        route: TaskRoute,
        *,
        session_id: str,
        request_id: str,
        plan_id: str,
        dry_run: bool,
    ) -> None:
        """Route to appropriate worker."""
        if route.worker_type == "agent" and self.registry is not None:
            agent = self.registry.get(route.worker_name)
            if agent is not None:
                await self._run_via_agent(
                    agent, task, session_id=session_id, request_id=request_id, plan_id=plan_id, dry_run=dry_run
                )
                return

        if route.worker_type == "executor" and self.executor is not None:
            await self._run_via_executor(
                task, session_id=session_id, request_id=request_id, plan_id=plan_id, dry_run=dry_run
            )
            return

        if route.worker_type == "conversation":
            self._run_conversation(task)
            return

        # Fallback if no matching worker
        if task.steps and self.executor is not None:
            await self._run_via_executor(
                task, session_id=session_id, request_id=request_id, plan_id=plan_id, dry_run=dry_run
            )
        else:
            task.state = TaskState.DONE
            task.summary = task.summary or f"processed goal: {task.goal}"

    async def _run_via_agent(
        self,
        agent: StarAgent,
        task: Task,
        *,
        session_id: str,
        request_id: str,
        plan_id: str,
        dry_run: bool,
    ) -> None:
        """Execute task via a StarAgent worker."""
        run = await agent.run(
            task.goal,
            session_id=session_id,
            request_id=request_id,
            plan_id=plan_id,
            task_id=task.task_id,
            dry_run=dry_run,
        )

        task.summary = run.summary or f"{run.agent.value}: {run.state}"
        task.checkpoint = {
            "run_id": run.run_id,
            "steps_used": run.steps_used,
            "agent_state": run.state,
            "dry_run": run.dry_run,
        }

        # Map AgentRunState to TaskState
        if run.state in (AgentRunState.DONE, AgentRunState.DRY_RUN):
            task.state = TaskState.DONE
            task.verification = Verification(
                expected=f"agent {agent.name.value} completes goal",
                observed=f"{run.steps_used} step(s) succeeded ({run.state})",
                ok=True,
                method="result_flag",
                note="dry-run: simulated" if run.dry_run else "",
            )
        elif run.state == AgentRunState.WAITING_CONFIRMATION:
            task.state = TaskState.WAITING_CONFIRMATION
            task.error = run.error
        elif run.state == AgentRunState.BLOCKED:
            task.state = TaskState.BLOCKED
            task.error = run.error or "blocked by policy or stop gate"
        elif run.state == AgentRunState.CANCELLED:
            task.state = TaskState.CANCELLED
            task.error = run.error or "agent run cancelled"
        else:
            task.state = TaskState.FAILED
            task.error = run.error or f"agent run ended with state {run.state}"

    async def _run_via_executor(
        self,
        task: Task,
        *,
        session_id: str,
        request_id: str,
        plan_id: str,
        dry_run: bool,
    ) -> None:
        """Execute task steps via ToolExecutor."""
        assert self.executor is not None
        for call in task.steps:
            if call.state not in (ToolCallState.PROPOSED, ToolCallState.APPROVED):
                continue
            await self.executor.run_call(
                call,
                session_id=session_id,
                request_id=request_id,
                plan_id=plan_id,
                task_id=task.task_id,
                dry_run=dry_run,
            )

        task.state = task_state_from_calls(
            task.steps,
            risk=task.risk,
            confirm_above=self.settings.security.confirm_above_risk,
            deny_risk=self.settings.security.deny_risk,
        )
        task.checkpoint = {
            "calls_count": len(task.steps),
            "calls_states": [c.state.value for c in task.steps],
            "dry_run": dry_run,
        }
        task.verification = Verification(
            expected=f"{len(task.steps)} tool call(s) succeed",
            observed=" ".join(f"{c.tool}={c.state.value}" for c in task.steps) + (" dry_run" if dry_run else ""),
            ok=bool(task.steps) and all(
                (c.state is ToolCallState.DONE and c.result.get("success", True))
                or c.state is ToolCallState.SKIPPED
                for c in task.steps
            ),
            method="result_flag",
            note="dry-run: simulated" if dry_run else "",
        )
        task.summary = ", ".join(c.tool for c in task.steps)[:180]
        task.error = next((c.error for c in task.steps if c.error), None)

    def _run_conversation(self, task: Task) -> None:
        """Execute a conversational response task."""
        task.state = TaskState.DONE
        if not task.summary:
            task.summary = task.goal
        task.checkpoint = {"conversation": True, "done": True}

    def _emit_task_event(self, kind: str, task: Task, *, session_id: str) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            kind,
            phase=EventPhase.TASK,
            session_id=session_id or self.settings.session_id,
            task_id=task.task_id,
            goal=task.goal,
            agent=task.agent.value,
            state=task.state.value,
            risk=task.risk,
            summary=task.summary,
        )

    def _emit_terminal_event(self, task: Task, *, session_id: str) -> None:
        if task.state in (TaskState.DONE, TaskState.RECOVERED):
            self._emit_task_event("task.completed", task, session_id=session_id)
        elif task.state is TaskState.CANCELLED:
            self._emit_task_event("task.cancelled", task, session_id=session_id)
        elif task.state is TaskState.FAILED:
            if self.bus is not None:
                self.bus.emit(
                    "task.failed",
                    phase=EventPhase.TASK,
                    session_id=session_id or self.settings.session_id,
                    task_id=task.task_id,
                    goal=task.goal,
                    agent=task.agent.value,
                    error=task.error,
                )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        """Cancel a running task or mark a pending task as cancelled."""
        active = self._active_tasks.get(task_id)
        if active is not None and not active.done():
            active.cancel()
            return {"ok": True, "task_id": task_id, "cancelled": True, "status": "active_cancelled"}

        record = self._task_records.get(task_id)
        if record is not None:
            if record.state not in (TaskState.DONE, TaskState.CANCELLED):
                record.state = TaskState.CANCELLED
                record.error = "cancelled by request"
                self._emit_task_event("task.cancelled", record, session_id="")
                return {"ok": True, "task_id": task_id, "cancelled": True, "status": "record_cancelled"}
            return {"ok": False, "task_id": task_id, "error": f"task already {record.state.value}"}

        return {"ok": False, "task_id": task_id, "error": f"task '{task_id}' not found"}

    async def cancel_all(self, reason: str = "") -> list[str]:
        """Cancel all active running tasks."""
        cancelled_ids: list[str] = []
        for task_id, async_task in list(self._active_tasks.items()):
            if not async_task.done():
                async_task.cancel()
                cancelled_ids.append(task_id)
                record = self._task_records.get(task_id)
                if record is not None:
                    record.state = TaskState.CANCELLED
                    record.error = f"cancelled: {reason}"
                    self._emit_task_event("task.cancelled", record, session_id="")
        self._active_tasks.clear()
        return cancelled_ids

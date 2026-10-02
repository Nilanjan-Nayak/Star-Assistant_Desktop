"""The STAR 2.0 advanced orchestration engine (Phase 10).

The Orchestrator is the conductor of the multi-agent system. It takes a
plan from the brain, sequences its tasks across goal-oriented agents and
tools, manages checkpoints (pause / resume / rollback), enforces honest
recovery, and guarantees graceful cancellation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from Backend.star.agents.base import AgentRegistry
from Backend.star.brain.schemas import Context, Plan, Task, TaskState
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.orchestrator.checkpoint import CheckpointStore
from Backend.star.orchestrator.recovery import RecoveryManager
from Backend.star.orchestrator.router import PlanRouter
from Backend.star.orchestrator.runner import TaskRunner
from Backend.star.orchestrator.schemas import (
    CheckpointStatus,
    OrchestratorSnapshot,
    PlanCheckpoint,
)
from Backend.star.tools.executor import ToolExecutor

_log = star_logger("orchestrator.engine")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    """Multi-agent orchestrator: routing, checkpoints, recovery, cancellation."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        executor: ToolExecutor | None = None,
        registry: AgentRegistry | None = None,
        workspace: Any = None,
        stop_gate: Callable[[], bool] | None = None,
        checkpoints_path: Path | str | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.executor = executor
        self.registry = registry
        self.workspace = workspace
        self.stop_gate = stop_gate
        self.name = "orchestrator"

        store_path = checkpoints_path or settings.paths.checkpoints_path
        self.checkpoints = CheckpointStore(
            store_path,
            max_records=settings.orchestrator.max_checkpoints,
            bus=bus,
        )
        self.recovery = RecoveryManager(settings, bus=bus)
        self.router = PlanRouter(registry=registry, executor=executor)
        self.runner = TaskRunner(
            settings,
            bus=bus,
            executor=executor,
            registry=registry,
            recovery=self.recovery,
            stop_gate=stop_gate,
        )

        # Internal ledgers for tasks & plans (backing app.tasks() and app.plans())
        self._plans_ledger: dict[str, dict[str, Any]] = {}
        self._tasks_ledger: dict[str, dict[str, Any]] = {}
        self._paused_plans: set[str] = set()
        self._active_plans: dict[str, Plan] = {}

        self.stats: dict[str, int] = {
            "plans_executed": 0,
            "tasks_executed": 0,
            "tasks_failed": 0,
            "tasks_blocked": 0,
            "tasks_recovered": 0,
            "tasks_cancelled": 0,
            "checkpoints_created": 0,
            "checkpoints_restored": 0,
            "plans_paused": 0,
            "plans_resumed": 0,
        }

    # ── execution seam (invoked by brain or directly) ─────────────────────────
    async def execute(
        self,
        plan: Plan,
        *,
        context: Context | None = None,
        session_id: str = "",
        request_id: str = "",
        dry_run: bool | None = None,
    ) -> Plan:
        """Run a plan through the orchestrator. Satisfies the brain pipeline seam."""
        effective_session = session_id or plan.request_id or self.settings.session_id
        effective_request = request_id or plan.request_id
        effective_dry_run = self.settings.security.dry_run if dry_run is None else bool(dry_run)

        self._active_plans[plan.plan_id] = plan
        self.stats["plans_executed"] += 1

        # Initial checkpoint
        self._create_checkpoint(plan, label="plan_started", status=CheckpointStatus.ACTIVE)

        # Sequence tasks by priority
        sequenced_tasks = self.router.plan_sequence(plan)

        executed = denied = waiting = failed = recovered = cancelled = 0

        for index, task in enumerate(sequenced_tasks):
            # Check if plan was paused
            if plan.plan_id in self._paused_plans:
                _log.info("star2.orchestrator.plan_paused plan_id=%s at step=%d", plan.plan_id, index)
                self._create_checkpoint(plan, label=f"paused_at_step_{index}", status=CheckpointStatus.PAUSED)
                break

            # Check stop gate
            if self.is_stopped():
                task.state = TaskState.BLOCKED
                task.error = "emergency stop active during plan execution"
                self.stats["tasks_blocked"] += 1
                denied += 1
                self.record_task(task)
                break

            # Route and run
            route = self.router.route_task(task)
            await self.runner.run_task(
                task,
                route,
                session_id=effective_session,
                request_id=effective_request,
                plan_id=plan.plan_id,
                dry_run=effective_dry_run,
            )

            # Record in ledger
            self.record_task(task)
            self.stats["tasks_executed"] += 1

            # Tally
            if task.state is TaskState.DONE:
                executed += 1
            elif task.state is TaskState.RECOVERED:
                recovered += 1
                self.stats["tasks_recovered"] += 1
            elif task.state is TaskState.BLOCKED:
                denied += 1
                self.stats["tasks_blocked"] += 1
            elif task.state is TaskState.WAITING_CONFIRMATION:
                waiting += 1
            elif task.state is TaskState.CANCELLED:
                cancelled += 1
                self.stats["tasks_cancelled"] += 1
            elif task.state is TaskState.FAILED:
                failed += 1
                self.stats["tasks_failed"] += 1

            # Intermediate checkpoint
            if self.settings.orchestrator.checkpoint_enabled:
                self._create_checkpoint(
                    plan,
                    label=f"step_{index}_{task.agent.value}",
                    status=CheckpointStatus.ACTIVE,
                    step_index=index + 1,
                )

            # Stop further execution if waiting confirmation or critical failure
            if task.state is TaskState.WAITING_CONFIRMATION:
                plan.requires_confirmation = True
                break

        # Final checkpoint
        final_status = (
            CheckpointStatus.PAUSED
            if plan.plan_id in self._paused_plans
            else CheckpointStatus.COMPLETED
            if plan.all_terminal
            else CheckpointStatus.ACTIVE
        )
        self._create_checkpoint(plan, label="plan_ended", status=final_status)

        # Emit plan.executed event
        if self.bus is not None:
            self.bus.emit(
                "plan.executed",
                phase=EventPhase.PLAN,
                plan_id=plan.plan_id,
                request_id=effective_request,
                session_id=effective_session,
                executed=executed,
                denied=denied,
                waiting_confirmation=waiting,
                failed=failed,
                recovered=recovered,
                cancelled=cancelled,
                dry_run=effective_dry_run,
            )

        self.record_plan(plan)
        self._active_plans.pop(plan.plan_id, None)
        return plan

    async def execute_plan(
        self,
        plan: Plan,
        *,
        context: Context | None = None,
        session_id: str = "",
        request_id: str = "",
        dry_run: bool | None = None,
    ) -> Plan:
        """Alias for :meth:`execute`."""
        return await self.execute(
            plan, context=context, session_id=session_id, request_id=request_id, dry_run=dry_run
        )

    async def run_task(
        self,
        task: Task,
        *,
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        dry_run: bool | None = None,
    ) -> Task:
        """Run an individual task."""
        route = self.router.route_task(task)
        res = await self.runner.run_task(
            task,
            route,
            session_id=session_id,
            request_id=request_id,
            plan_id=plan_id,
            dry_run=dry_run,
        )
        self.record_task(res)
        return res

    # ── checkpoints & pause / resume / rollback ───────────────────────────────
    def _create_checkpoint(
        self,
        plan: Plan,
        *,
        label: str = "",
        status: CheckpointStatus = CheckpointStatus.ACTIVE,
        step_index: int = 0,
    ) -> PlanCheckpoint:
        task_states = {t.task_id: t.state for t in plan.tasks}
        task_checkpoints = {t.task_id: dict(t.checkpoint) for t in plan.tasks if t.checkpoint}
        completed = [t.task_id for t in plan.tasks if t.state in (TaskState.DONE, TaskState.RECOVERED)]
        pending = [t.task_id for t in plan.tasks if t.state is TaskState.PENDING]
        failed = [t.task_id for t in plan.tasks if t.state is TaskState.FAILED]
        cancelled = [t.task_id for t in plan.tasks if t.state is TaskState.CANCELLED]

        chk = PlanCheckpoint(
            plan_id=plan.plan_id,
            label=label,
            status=status,
            step_index=step_index,
            task_states=task_states,
            task_checkpoints=task_checkpoints,
            completed_tasks=completed,
            pending_tasks=pending,
            failed_tasks=failed,
            cancelled_tasks=cancelled,
            metadata={"intent": plan.intent, "task_count": len(plan.tasks)},
        )
        saved = self.checkpoints.save(chk)
        self.stats["checkpoints_created"] += 1
        return saved

    def pause_plan(self, plan_id: str) -> dict[str, Any]:
        """Signal an active plan to pause execution at the next step."""
        self._paused_plans.add(plan_id)
        self.stats["plans_paused"] += 1
        plan = self._active_plans.get(plan_id)
        chk = None
        if plan is not None:
            chk = self._create_checkpoint(plan, label="manual_pause", status=CheckpointStatus.PAUSED)
        if self.bus is not None:
            self.bus.emit("plan.paused", phase=EventPhase.PLAN, plan_id=plan_id)
        return {"ok": True, "plan_id": plan_id, "paused": True, "checkpoint_id": chk.checkpoint_id if chk else None}

    async def resume_plan(
        self,
        plan_id: str,
        plan: Plan | None = None,
        *,
        session_id: str = "",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Resume a paused plan from its latest checkpoint."""
        self._paused_plans.discard(plan_id)
        self.stats["plans_resumed"] += 1
        latest_chk = self.checkpoints.latest(plan_id)

        target_plan = plan or self._active_plans.get(plan_id)
        if target_plan is None and latest_chk is not None:
            # Reconstruct plan if record exists
            plan_record = self._plans_ledger.get(plan_id)
            if plan_record:
                target_plan = Plan.model_validate(plan_record)

        if target_plan is None:
            return {"ok": False, "plan_id": plan_id, "error": f"plan '{plan_id}' not found to resume"}

        # Restore task states from checkpoint if available
        if latest_chk is not None:
            self.stats["checkpoints_restored"] += 1
            for task in target_plan.tasks:
                if task.task_id in latest_chk.task_states:
                    task.state = latest_chk.task_states[task.task_id]

        if self.bus is not None:
            self.bus.emit("plan.resumed", phase=EventPhase.PLAN, plan_id=plan_id)

        # Continue execution asynchronously
        asyncio.create_task(
            self.execute(target_plan, session_id=session_id, dry_run=dry_run)
        )
        return {
            "ok": True,
            "plan_id": plan_id,
            "resumed": True,
            "from_checkpoint": latest_chk.checkpoint_id if latest_chk else None,
        }

    def rollback_plan(self, plan_id: str, checkpoint_id: str) -> dict[str, Any]:
        """Restore a plan to an earlier checkpoint state."""
        chk = self.checkpoints.get(checkpoint_id)
        if chk is None or chk.plan_id != plan_id:
            return {"ok": False, "error": f"checkpoint '{checkpoint_id}' not found for plan '{plan_id}'"}

        plan = self._active_plans.get(plan_id)
        if plan is not None:
            for task in plan.tasks:
                if task.task_id in chk.task_states:
                    task.state = chk.task_states[task.task_id]
                if task.task_id in chk.task_checkpoints:
                    task.checkpoint = dict(chk.task_checkpoints[task.task_id])
            self.record_plan(plan)

        self.stats["checkpoints_restored"] += 1
        return {"ok": True, "plan_id": plan_id, "checkpoint_id": checkpoint_id, "rolled_back": True}

    # ── cancellation & stop gate ──────────────────────────────────────────────
    def is_stopped(self) -> bool:
        return bool(self.stop_gate()) if callable(self.stop_gate) else False

    async def cancel(self, task_id: str) -> dict[str, Any]:
        """Cancel a running task."""
        res = await self.runner.cancel(task_id)
        if res.get("ok"):
            self.stats["tasks_cancelled"] += 1
        return res

    async def cancel_all(self, reason: str = "") -> list[str]:
        """Cancel all running tasks (used by emergency_stop)."""
        cancelled = await self.runner.cancel_all(reason=reason)
        self.stats["tasks_cancelled"] += len(cancelled)
        return cancelled

    # ── ledgers & query interface ─────────────────────────────────────────────
    def tasks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return recorded tasks for app.tasks()."""
        all_tasks = list(self._tasks_ledger.values())
        return all_tasks[-limit:] if limit else all_tasks

    def plans(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return recorded plans for app.plans()."""
        all_plans = list(self._plans_ledger.values())
        return all_plans[-limit:] if limit else all_plans

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Lookup a single plan by id."""
        return self._plans_ledger.get(plan_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Lookup a single task by id."""
        return self._tasks_ledger.get(task_id)

    def record_plan(self, plan: dict[str, Any] | Plan) -> None:
        """Absorb a plan record into the ledger."""
        data = plan.public() if isinstance(plan, Plan) else dict(plan)
        plan_id = str(data.get("plan_id") or "")
        if plan_id:
            self._plans_ledger[plan_id] = data
            if len(self._plans_ledger) > 200:
                oldest = next(iter(self._plans_ledger))
                del self._plans_ledger[oldest]
        for task in data.get("tasks") or []:
            if isinstance(task, dict):
                self.record_task(task)

    def record_task(self, task: dict[str, Any] | Task) -> None:
        """Absorb a task record into the ledger."""
        data = task.public() if isinstance(task, Task) else dict(task)
        task_id = str(data.get("task_id") or "")
        if task_id:
            self._tasks_ledger[task_id] = data
            if len(self._tasks_ledger) > 500:
                oldest = next(iter(self._tasks_ledger))
                del self._tasks_ledger[oldest]

    # ── lifecycle & diagnostics ───────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if self.is_stopped():
            problems.append("emergency stop is active")
        active_count = len(self.runner._active_tasks)
        return {
            "status": "degraded" if problems else "ok",
            "detail": {
                "active_tasks": active_count,
                "paused_plans": len(self._paused_plans),
                "total_plans": len(self._plans_ledger),
                "total_tasks": len(self._tasks_ledger),
                "stats": dict(self.stats),
                "problems": problems,
            },
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "active_plans": len(self._active_plans),
            "active_tasks": len(self.runner._active_tasks),
            "checkpoints_count": self.checkpoints.count(),
            "paused_plans": list(self._paused_plans),
            "stats": dict(self.stats),
        }

    async def startup(self) -> None:
        _log.info("star2.orchestrator.startup")

    async def aclose(self) -> None:
        await self.cancel_all(reason="orchestrator_shutdown")
        self.checkpoints.close()
        _log.info("star2.orchestrator.shutdown")

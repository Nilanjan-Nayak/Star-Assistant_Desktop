"""Orchestration tools for Phase 10.

Allows agents or operators to inspect task statuses, trigger graceful
cancellations, pause/resume plans, or record plan checkpoints.
"""

from __future__ import annotations

import asyncio
from typing import Any

from Backend.star.brain.schemas import AgentName
from Backend.star.config.settings import Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.orchestrator.engine import Orchestrator
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.tools.spec import ToolCategory, ToolSpec

_log = star_logger("orchestrator.tools")


class OrchestratorToolkit:
    """Handlers for orchestration control and introspection tools."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.settings = settings
        self.bus = bus

    def task_cancel(self, task_id: str) -> dict[str, Any]:
        """Cancel a running task by id."""
        # Sync wrapper around async cancel
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(self.orchestrator.cancel(task_id), loop)
            return future.result(timeout=5.0)
        except RuntimeError:
            return asyncio.run(self.orchestrator.cancel(task_id))

    def task_status(self, task_id: str) -> dict[str, Any]:
        """Inspect the current status and checkpoint of a task."""
        record = self.orchestrator.get_task(task_id)
        if record is None:
            return {"ok": False, "task_id": task_id, "error": f"task '{task_id}' not found"}
        return {"ok": True, "task": record}

    def plan_pause(self, plan_id: str) -> dict[str, Any]:
        """Pause execution of an active plan."""
        return self.orchestrator.pause_plan(plan_id)

    def plan_resume(self, plan_id: str) -> dict[str, Any]:
        """Resume execution of a paused plan."""
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(self.orchestrator.resume_plan(plan_id), loop)
            return future.result(timeout=5.0)
        except RuntimeError:
            return asyncio.run(self.orchestrator.resume_plan(plan_id))

    def plan_checkpoint(self, plan_id: str, label: str = "") -> dict[str, Any]:
        """Create a manual checkpoint for a plan."""
        plan_record = self.orchestrator.get_plan(plan_id)
        if plan_record is None:
            return {"ok": False, "plan_id": plan_id, "error": f"plan '{plan_id}' not found"}
        from Backend.star.brain.schemas import Plan

        plan = Plan.model_validate(plan_record)
        chk = self.orchestrator._create_checkpoint(plan, label=label or "manual")
        return {"ok": True, "plan_id": plan_id, "checkpoint_id": chk.checkpoint_id}

    def orchestrator_status(self) -> dict[str, Any]:
        """Return runtime metrics and operational state of the orchestrator."""
        return {"ok": True, **self.orchestrator.describe()}


def register_orchestrator_tools(
    registry: StarToolRegistry,
    settings: Settings,
    *,
    bus: StarEventBus | None = None,
    orchestrator: Orchestrator,
) -> list[ToolSpec]:
    """Register orchestration tools in the Phase 4 typed tool registry."""
    toolkit = OrchestratorToolkit(orchestrator, settings, bus=bus)

    specs = [
        ToolSpec(
            name="task_cancel",
            category=ToolCategory.META,
            risk="medium",
            agent=AgentName.SYSTEM,
            description="Cancel a running task by id",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "ID of the task to cancel"}},
                "required": ["task_id"],
            },
            handler=toolkit.task_cancel,
        ),
        ToolSpec(
            name="task_status",
            category=ToolCategory.META,
            risk="low",
            agent=AgentName.SYSTEM,
            description="Inspect the status and checkpoint of a task",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "ID of the task to inspect"}},
                "required": ["task_id"],
            },
            handler=toolkit.task_status,
        ),
        ToolSpec(
            name="plan_pause",
            category=ToolCategory.META,
            risk="medium",
            agent=AgentName.SYSTEM,
            description="Pause execution of an active plan",
            parameters={
                "type": "object",
                "properties": {"plan_id": {"type": "string", "description": "ID of the plan to pause"}},
                "required": ["plan_id"],
            },
            handler=toolkit.plan_pause,
        ),
        ToolSpec(
            name="plan_resume",
            category=ToolCategory.META,
            risk="medium",
            agent=AgentName.SYSTEM,
            description="Resume execution of a paused plan",
            parameters={
                "type": "object",
                "properties": {"plan_id": {"type": "string", "description": "ID of the plan to resume"}},
                "required": ["plan_id"],
            },
            handler=toolkit.plan_resume,
        ),
        ToolSpec(
            name="plan_checkpoint",
            category=ToolCategory.META,
            risk="low",
            agent=AgentName.SYSTEM,
            description="Create a manual checkpoint for an active plan",
            parameters={
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "ID of the plan to snapshot"},
                    "label": {"type": "string", "description": "Optional label for the checkpoint", "default": ""},
                },
                "required": ["plan_id"],
            },
            handler=toolkit.plan_checkpoint,
        ),
        ToolSpec(
            name="orchestrator_status",
            category=ToolCategory.META,
            risk="low",
            agent=AgentName.SYSTEM,
            description="Return active tasks, queue metrics, and orchestration stats",
            parameters={"type": "object", "properties": {}},
            handler=toolkit.orchestrator_status,
        ),
    ]

    for spec in specs:
        registry.register(spec)

    _log.info("star2.orchestrator.tools.registered count=%d", len(specs))
    return specs

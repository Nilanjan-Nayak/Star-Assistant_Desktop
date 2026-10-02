"""Typed schemas for Phase 10 — advanced orchestration.

These schemas govern multi-agent task routing, plan execution state,
recovery decisions, and the checkpoint system for pause/resume/rollback.
"""

from __future__ import annotations

from datetime import datetime, timezone
import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from Backend.star.brain.schemas import AgentName, TaskState, ToolCall


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_checkpoint_id() -> str:
    from agent.core.ids import new_id

    return new_id("chk")


@enum.unique
class CheckpointStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanCheckpoint(BaseModel):
    """Snapshot of a plan's state at a point in time.

    Enables pausing execution, resuming from the latest checkpoint,
    or rolling back after partial failures (blueprint §7: checkpoints).
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str = Field(default_factory=new_checkpoint_id)
    plan_id: str
    label: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    status: CheckpointStatus = CheckpointStatus.ACTIVE
    step_index: int = 0
    task_states: dict[str, TaskState] = Field(default_factory=dict)
    task_checkpoints: dict[str, dict[str, Any]] = Field(default_factory=dict)
    task_results: dict[str, Any] = Field(default_factory=dict)
    completed_tasks: list[str] = Field(default_factory=list)
    pending_tasks: list[str] = Field(default_factory=list)
    failed_tasks: list[str] = Field(default_factory=list)
    cancelled_tasks: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["created_at"] = self.created_at.isoformat()
        return data


class TaskRoute(BaseModel):
    """Routing decision for a single task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    agent: AgentName
    worker_type: str = "agent"              # agent | executor | conversation
    worker_name: str = ""                   # e.g. "browser", "computer", "tool_executor"
    priority: int = 5
    goal: str = ""
    steps: list[ToolCall] = Field(default_factory=list)
    requires_confirmation: bool = False


class OrchestratorSnapshot(BaseModel):
    """Diagnostic state of the orchestration engine."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    active_plans: int = 0
    active_tasks: int = 0
    total_plans: int = 0
    total_tasks: int = 0
    executed_tasks: int = 0
    failed_tasks: int = 0
    blocked_tasks: int = 0
    recovered_tasks: int = 0
    cancelled_tasks: int = 0
    checkpoints_saved: int = 0
    checkpoints_restored: int = 0
    paused_plans: list[str] = Field(default_factory=list)
    workers: list[str] = Field(default_factory=list)

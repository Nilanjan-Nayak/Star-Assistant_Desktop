"""STAR 2.0 advanced orchestration package (Phase 10).

Provides multi-agent routing, checkpointing for pause/resume/rollback,
bounded honest recovery, and graceful task cancellation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from Backend.star.agents.base import AgentRegistry
from Backend.star.config.settings import Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.orchestrator.checkpoint import CheckpointStore
from Backend.star.orchestrator.engine import Orchestrator
from Backend.star.orchestrator.recovery import RecoveryManager
from Backend.star.orchestrator.router import PlanRouter
from Backend.star.orchestrator.runner import TaskRunner
from Backend.star.orchestrator.schemas import (
    CheckpointStatus,
    OrchestratorSnapshot,
    PlanCheckpoint,
    TaskRoute,
)
from Backend.star.orchestrator.tools import register_orchestrator_tools
from Backend.star.tools.executor import ToolExecutor

__all__ = [
    "CheckpointStatus",
    "CheckpointStore",
    "Orchestrator",
    "OrchestratorSnapshot",
    "PlanCheckpoint",
    "PlanRouter",
    "RecoveryManager",
    "TaskRoute",
    "TaskRunner",
    "build_orchestrator",
    "register_orchestrator_tools",
]


def build_orchestrator(
    settings: Settings,
    *,
    bus: StarEventBus | None = None,
    executor: ToolExecutor | None = None,
    registry: AgentRegistry | None = None,
    workspace: Any = None,
    stop_gate: Callable[[], bool] | None = None,
    checkpoints_path: Path | str | None = None,
) -> Orchestrator:
    """Construct and configure the Phase 10 Orchestrator."""
    return Orchestrator(
        settings,
        bus=bus,
        executor=executor,
        registry=registry,
        workspace=workspace,
        stop_gate=stop_gate,
        checkpoints_path=checkpoints_path,
    )

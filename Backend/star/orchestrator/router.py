"""Task sequencing and agent routing for Phase 10 orchestration.

Maps each task in a plan to the best worker (a specialised StarAgent,
the ToolExecutor, or the conversation handler), and determines priority order.
"""

from __future__ import annotations

from typing import Any

from Backend.star.agents.base import AgentRegistry, StarAgent
from Backend.star.brain.schemas import AgentName, Plan, Task
from Backend.star.observability.logging import star_logger
from Backend.star.orchestrator.schemas import TaskRoute
from Backend.star.tools.executor import ToolExecutor

_log = star_logger("orchestrator.router")


class PlanRouter:
    """Decides agent/tool sequence and worker mapping for plan tasks."""

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        executor: ToolExecutor | None = None,
    ) -> None:
        self.registry = registry
        self.executor = executor

    def plan_sequence(self, plan: Plan) -> list[Task]:
        """Sequence tasks by priority (lowest number = highest priority).

        Python's Timsort is stable: tasks with identical priority preserve
        their original order within the plan.
        """
        return sorted(plan.tasks, key=lambda t: t.priority)

    def route_task(self, task: Task) -> TaskRoute:
        """Resolve which worker will execute this task."""
        # 1. Conversational / response tasks
        if task.agent is AgentName.CONVERSATION:
            return TaskRoute(
                task_id=task.task_id,
                agent=task.agent,
                worker_type="conversation",
                worker_name="conversation",
                priority=task.priority,
                goal=task.goal,
                steps=task.steps,
            )

        # 2. Explicit registered agent match
        if self.registry is not None:
            agent = self.registry.get(task.agent)
            if agent is not None:
                return TaskRoute(
                    task_id=task.task_id,
                    agent=task.agent,
                    worker_type="agent",
                    worker_name=task.agent.value,
                    priority=task.priority,
                    goal=task.goal,
                    steps=task.steps,
                )

            # 3. Dynamic dispatch based on goal scoring
            dispatched = self.registry.dispatch(task.goal)
            if dispatched is not None:
                return TaskRoute(
                    task_id=task.task_id,
                    agent=dispatched.name,
                    worker_type="agent",
                    worker_name=dispatched.name.value,
                    priority=task.priority,
                    goal=task.goal,
                    steps=task.steps,
                )

        # 4. Tool calls route to ToolExecutor
        if task.steps and self.executor is not None:
            return TaskRoute(
                task_id=task.task_id,
                agent=task.agent,
                worker_type="executor",
                worker_name="tool_executor",
                priority=task.priority,
                goal=task.goal,
                steps=task.steps,
            )

        # 5. Default fallback
        return TaskRoute(
            task_id=task.task_id,
            agent=task.agent,
            worker_type="executor",
            worker_name="tool_executor" if self.executor is not None else "noop",
            priority=task.priority,
            goal=task.goal,
            steps=task.steps,
        )

"""Planning — turn reasoning into a structured, inspectable :class:`Plan`.

Blueprint §7 Phase 3: "Generate structured task plans with states
(pending/running/verifying/done/failed/cancelled/recovered)" and §4:
"Orchestrator sequences agents; agents do the work; tools are capabilities."

Design notes
------------
* The planner is **pure**: it reads a :class:`Reasoning` + :class:`Context` and
  produces a :class:`Plan`. It never touches the OS.
* Actions the legacy pipeline already executed (the deterministic router runs its
  tools while routing) become :class:`ToolCall` entries with their real result, so
  the plan is an honest record and reflection can verify it.
* LLM *proposals* become ``PROPOSED`` calls — they are executed only by the
  orchestrator (Phase 10) through the tool registry + policy (Phase 4/11).
* Risk is a pluggable classifier. Until the typed registry (Phase 4) and the policy
  engine (Phase 11) land, a keyword-based default keeps confirmations meaningful.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from Backend.star.brain.schemas import (
    AgentName,
    Context,
    Plan,
    Prediction,
    RawAction,
    Reasoning,
    Task,
    TaskState,
    ToolCall,
    ToolCallState,
    UserRequest,
    Verification,
)
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.tools.risk import (  # noqa: F401 — re-exported for compatibility
    RISK_KEYWORDS,
    RISK_ORDER,
    classify_risk,
    default_risk,
    highest,
    risk_at_least,
    risk_rank,
)
from Backend.star.tools.spec import PREFIX_AGENT, TOOL_AGENT, agent_for_tool  # noqa: F401 — re-exported

__all__ = [
    "PREFIX_AGENT",
    "RISK_KEYWORDS",
    "RISK_ORDER",
    "StarPlanner",
    "TOOL_AGENT",
    "agent_for_tool",
    "classify_risk",
    "default_risk",
    "highest",
    "risk_at_least",
    "risk_rank",
    "task_state_from_calls",
]

_log = star_logger("planning")

RISK_ORDER: tuple[str, ...] = ("unknown", "low", "medium", "high", "critical")

def task_state_from_calls(
    calls: Iterable[ToolCall],
    *,
    risk: str = "low",
    confirm_above: str = "high",
    deny_risk: str = "critical",
) -> TaskState:
    """Derive a task's state from its tool calls (used by the planner *and* the executor)."""
    items = list(calls)
    if not items:
        return TaskState.PENDING
    states = {call.state for call in items}
    if ToolCallState.DENIED in states or risk_at_least(risk, deny_risk):
        return TaskState.BLOCKED
    if states <= {ToolCallState.DONE, ToolCallState.SKIPPED}:
        return TaskState.DONE if ToolCallState.DONE in states else TaskState.PENDING
    if ToolCallState.FAILED in states:
        return TaskState.FAILED
    if any(call.state is ToolCallState.PROPOSED and call.requires_confirmation for call in items):
        return TaskState.WAITING_CONFIRMATION
    if risk_at_least(risk, confirm_above) and ToolCallState.PROPOSED in states:
        return TaskState.WAITING_CONFIRMATION
    if states & {ToolCallState.RUNNING, ToolCallState.APPROVED}:
        return TaskState.RUNNING
    return TaskState.PENDING


class StarPlanner:
    """Builds :class:`Plan` objects from reasoning + context."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        risk_classifier: Callable[[str, dict[str, Any]], str] | None = None,
        max_tasks: int | None = None,
        max_calls_per_task: int | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self.risk_classifier = risk_classifier or default_risk
        self.max_tasks = max_tasks or settings.brain.max_plan_tasks
        self.max_calls_per_task = max_calls_per_task or settings.brain.max_tool_calls_per_task
        self.stats = {"plans": 0, "tasks": 0, "truncated": 0, "confirmations": 0, "blocked": 0}

    # ── helpers ───────────────────────────────────────────────────────────
    def risk_of(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        try:
            risk = str(self.risk_classifier(tool, arguments or {})).lower()
        except Exception as exc:  # noqa: BLE001 — a bad classifier must not break planning
            _log.warning("risk classifier failed for %s: %s", tool, exc)
            return "medium"
        return risk if risk in RISK_ORDER else "medium"

    def _needs_confirmation(self, risk: str) -> bool:
        return risk_at_least(risk, self.settings.security.confirm_above_risk)

    def _is_denied(self, risk: str) -> bool:
        return risk_at_least(risk, self.settings.security.deny_risk)

    def _call_from_action(self, action: RawAction) -> ToolCall:
        result = dict(action.result or {})
        ok = bool(result.get("success", result.get("ok", True)))
        state = ToolCallState.DONE if ok else ToolCallState.FAILED
        if action.executed is False:
            state = ToolCallState.PROPOSED
        return ToolCall(
            tool=action.tool,
            arguments=dict(action.args or {}),
            risk=self.risk_of(action.tool, action.args),
            state=state,
            attempt=1 if action.executed else 0,
            result=result,
            error=str(result.get("error") or "") or None if not ok else None,
        )

    def _call_from_proposal(self, proposal: dict[str, Any]) -> ToolCall:
        tool = str(proposal.get("tool") or "")
        arguments = dict(proposal.get("arguments") or {})
        risk = self.risk_of(tool, arguments)
        return ToolCall(
            tool=tool,
            arguments=arguments,
            risk=risk,
            state=ToolCallState.PROPOSED,
            requires_confirmation=self._needs_confirmation(risk),
        )

    # ── main entry ────────────────────────────────────────────────────────
    async def plan(
        self,
        request: UserRequest,
        context: Context,
        reasoning: Reasoning,
        *,
        predictions: Iterable[Prediction] | None = None,
    ) -> Plan:
        predicted = list(predictions or [])
        calls: list[ToolCall] = [self._call_from_action(action) for action in reasoning.actions]
        calls += [self._call_from_proposal(proposal) for proposal in reasoning.proposed_calls]

        if len(calls) > self.max_calls_per_task * self.max_tasks:
            self.stats["truncated"] += 1
            _log.warning("plan truncated from %d calls", len(calls))
            calls = calls[: self.max_calls_per_task * self.max_tasks]

        # group calls by owning agent — one task per agent, execution order preserved
        grouped: dict[AgentName, list[ToolCall]] = {}
        for call in calls:
            grouped.setdefault(agent_for_tool(call.tool), []).append(call)

        tasks: list[Task] = []
        for agent, agent_calls in grouped.items():
            for chunk_start in range(0, len(agent_calls), self.max_calls_per_task):
                chunk = agent_calls[chunk_start : chunk_start + self.max_calls_per_task]
                tasks.append(self._task_for(agent, chunk, context))

        # the conversational reply is always a task, so the plan explains itself
        reply_task = Task(
            goal="respond to the user",
            agent=AgentName.CONVERSATION,
            state=TaskState.DONE if reasoning.response_text else TaskState.PENDING,
            priority=1,
            risk="low",
            summary=reasoning.response_text[:280],
            verification=Verification(
                expected="a non-empty answer in the user's language",
                observed=f"{len(reasoning.response_text)} chars via {reasoning.source}",
                ok=bool(reasoning.response_text),
                method="output_match",
            ),
        )
        tasks.insert(0, reply_task)

        if len(tasks) > self.max_tasks:
            self.stats["truncated"] += 1
            tasks = tasks[: self.max_tasks]

        requires_confirmation = any(task.state is TaskState.WAITING_CONFIRMATION for task in tasks)
        plan = Plan(
            request_id=request.request_id,
            intent=reasoning.intent,
            rationale=self._rationale(reasoning, context, tasks),
            language=request.language,
            tasks=tasks,
            requires_confirmation=requires_confirmation,
            predicted_next=predicted,
            source=f"planner:{reasoning.source}",
        )
        self.stats["plans"] += 1
        self.stats["tasks"] += len(tasks)
        self.stats["confirmations"] += sum(1 for t in tasks if t.state is TaskState.WAITING_CONFIRMATION)
        self.stats["blocked"] += sum(1 for t in tasks if t.state is TaskState.BLOCKED)

        self.bus.emit(
            "plan.created",
            phase=EventPhase.BRAIN,
            request_id=request.request_id,
            session_id=request.session_id,
            plan_id=plan.plan_id,
            intent=plan.intent,
            tasks=len(tasks),
            agents=sorted({task.agent.value for task in tasks}),
            tool_calls=sum(len(task.steps) for task in tasks),
            requires_confirmation=requires_confirmation,
            predicted_next=len(predicted),
            risk=max((task.risk for task in tasks), key=lambda r: RISK_ORDER.index(r), default="low"),
        )
        return plan

    def _task_for(self, agent: AgentName, calls: list[ToolCall], context: Context) -> Task:
        risk = max(calls, key=lambda call: RISK_ORDER.index(call.risk)).risk if calls else "low"
        goal = ", ".join(call.tool for call in calls)[:180]
        state = task_state_from_calls(
            calls,
            risk=risk,
            confirm_above=self.settings.security.confirm_above_risk,
            deny_risk=self.settings.security.deny_risk,
        )

        expected = f"{len(calls)} call(s) for agent {agent.value} succeed"
        observed_parts = [f"{call.tool}={call.state.value}" for call in calls]
        ok = bool(calls) and all(
            (call.state is ToolCallState.DONE and call.result.get("success", call.result.get("ok", True)))
            or call.state is ToolCallState.SKIPPED
            for call in calls
        )
        if context.dry_run:
            observed_parts.append("dry_run")
        return Task(
            goal=f"{agent.value}: {goal}",
            agent=agent,
            steps=calls,
            state=state,
            risk=risk,
            priority=2 if state in (TaskState.WAITING_CONFIRMATION, TaskState.BLOCKED) else 5,
            summary=goal,
            error=next((call.error for call in calls if call.error), None),
            verification=Verification(
                expected=expected,
                observed=" ".join(observed_parts),
                ok=ok,
                method="result_flag",
                note="dry-run: nothing was really executed" if context.dry_run else "",
            ),
        )

    def _rationale(self, reasoning: Reasoning, context: Context, tasks: list[Task]) -> str:
        bits = [
            f"intent={reasoning.intent}",
            f"source={reasoning.source}",
            f"memory_hits={len(context.retrieved_memory)}",
            f"agents={','.join(sorted({task.agent.value for task in tasks}))}",
        ]
        if context.dry_run:
            bits.append("dry_run=true")
        if reasoning.notes:
            bits.append(f"sanitiser={reasoning.notes[:120]}")
        return "; ".join(bits)

    def describe(self) -> dict[str, Any]:
        return {
            "max_tasks": self.max_tasks,
            "max_calls_per_task": self.max_calls_per_task,
            "confirm_above_risk": self.settings.security.confirm_above_risk,
            "deny_risk": self.settings.security.deny_risk,
            "risk_classifier": getattr(self.risk_classifier, "__name__", repr(self.risk_classifier)),
            **self.stats,
        }

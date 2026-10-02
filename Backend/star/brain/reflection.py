"""Reflection — did it actually work, and what should Star do next?

Blueprint §3 (OBSERVE → ACT → VERIFY → RECOVER) and §7 Phase 3: "Add reflection
after execution: expected vs observed, retry or recover".

The reflector reads the plan's own evidence (tool-call states and results) plus an
optional verifier hook (Phase 6 supplies real screen/output verification) and
returns a :class:`Reflection` with an explicit recommendation. It never re-runs
anything itself — recovery is the orchestrator's job (Phase 10).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from Backend.star.brain.schemas import (
    AgentName,
    Context,
    Plan,
    Reasoning,
    Reflection,
    Task,
    TaskState,
    ToolCall,
    ToolCallState,
    UserRequest,
)
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

__all__ = ["NullReflector", "Reflector", "StarReflector", "call_succeeded"]

_log = star_logger("reflection")

Verifier = Callable[[Task, Context], Awaitable[tuple[bool, str]]]

_SUGGESTIONS_BN = {
    "success": "কাজটা ঠিকঠাক শেষ হয়েছে — আবার যাচাই করে নিলাম।",
    "partial": "কিছু অংশ হয়েছে, বাকিটা আবার চেষ্টা করতে হবে।",
    "failed": "কাজটা শেষ করা যায়নি — আমি আবার চেষ্টা করবো বা তোমার অনুমতি চাইবো।",
    "blocked": "এই কাজটা ঝুঁকিপূর্ণ, তাই নিরাপত্তার জন্য আটকে দিয়েছি।",
    "awaiting_confirmation": "এই কাজে তোমার হ্যাঁ দরকার — কনফার্ম করলেই আমি এগিয়ে যাবো।",
    "no_action": "কোনো টুল চালানোর দরকার পড়েনি, শুধু কথা বলেছি।",
    "unknown": "ফলাফল এখনো নিশ্চিত না।",
}
_SUGGESTIONS_EN = {
    "success": "Task completed and verified.",
    "partial": "Partly done — the rest needs another attempt.",
    "failed": "Could not finish this; I will retry or ask for permission.",
    "blocked": "This action is too risky, so it was blocked by policy.",
    "awaiting_confirmation": "This action needs your explicit confirmation.",
    "no_action": "No tool was needed — this was a conversation turn.",
    "unknown": "Outcome is not confirmed yet.",
}


def call_succeeded(call: ToolCall) -> bool:
    """A call counts as successful when it ran and its result says so."""
    if call.state is ToolCallState.SKIPPED:
        return True
    if call.state is not ToolCallState.DONE:
        return False
    result = call.result or {}
    if "success" in result:
        return bool(result["success"])
    if "ok" in result:
        return bool(result["ok"])
    return not call.error


@runtime_checkable
class Reflector(Protocol):
    name: str

    async def reflect(
        self, request: UserRequest, context: Context, plan: Plan, reasoning: Reasoning
    ) -> Reflection: ...


class NullReflector:
    name = "null"

    async def reflect(
        self, request: UserRequest, context: Context, plan: Plan, reasoning: Reasoning
    ) -> Reflection:
        return Reflection(ok=True, verdict="unknown", suggestion="reflection disabled")


class StarReflector:
    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self.verifier = verifier
        self.stats = {"reflections": 0, "success": 0, "failed": 0, "retries": 0, "recoveries": 0, "escalations": 0}

    async def reflect(
        self, request: UserRequest, context: Context, plan: Plan, reasoning: Reasoning
    ) -> Reflection:
        if not self.settings.brain.reflection_enabled:
            return Reflection(ok=True, verdict="unknown", suggestion="reflection disabled")

        work_tasks = [task for task in plan.tasks if task.steps]
        failed = [task for task in plan.tasks if task.state is TaskState.FAILED]
        blocked = [task for task in plan.tasks if task.state is TaskState.BLOCKED]
        waiting = [task for task in plan.tasks if task.state is TaskState.WAITING_CONFIRMATION]
        done = [task for task in plan.tasks if task.state is TaskState.DONE]

        evidence: list[str] = []
        for task in plan.tasks:
            for call in task.steps:
                result = call.result or {}
                evidence.append(
                    f"{call.tool}={call.state.value}"
                    + (f"(success={result['success']})" if "success" in result else "")
                    + (f"(error={str(call.error)[:80]})" if call.error else "")
                )
        evidence = evidence[:20]

        if blocked:
            verdict = "blocked"
        elif waiting:
            verdict = "awaiting_confirmation"
        elif failed and done and len(done) > len(failed):
            verdict = "partial"
        elif failed:
            verdict = "failed"
        elif work_tasks and all(task.state is TaskState.DONE for task in work_tasks):
            verdict = "success"
        elif not work_tasks:
            verdict = "no_action" if reasoning.response_text else "unknown"
        else:
            verdict = "unknown"

        expected = reasoning.response_text[:160] or f"intent {reasoning.intent} fulfilled"
        observed = "; ".join(f"{task.agent.value}:{task.state.value}" for task in plan.tasks)[:200] or "no tasks"

        retryable = [
            call
            for task in failed
            for call in task.steps
            if not call_succeeded(call)
            and task.retries < task.max_retries
            and call.risk in ("low", "medium", "unknown")
            and not context.dry_run
        ]
        should_retry = bool(retryable)
        should_recover = any(
            task.agent in (AgentName.COMPUTER, AgentName.BROWSER) and task.state is TaskState.FAILED
            for task in plan.tasks
        ) or any(task.state is TaskState.RECOVERED for task in plan.tasks)
        should_escalate = bool(blocked or waiting) or any(
            task.retries >= task.max_retries and task.state is TaskState.FAILED for task in plan.tasks
        )

        # optional real verification hook (screen diff / output match) — Phase 6 wires it
        if self.verifier is not None and verdict == "success" and work_tasks:
            try:
                ok, note = await self.verifier(work_tasks[0], context)
                if not ok:
                    verdict = "failed"
                    observed = f"{observed}; verifier: {note}"[:300]
            except Exception as exc:  # noqa: BLE001
                _log.warning("verifier failed: %s", exc)

        language = request.language if request.language in ("bn", "en") else "bn"
        table = _SUGGESTIONS_EN if language == "en" else _SUGGESTIONS_BN
        suggestion = table.get(verdict, table["unknown"])
        if should_retry:
            suggestion += " Retrying the failed step." if language == "en" else " ব্যর্থ ধাপটা আবার চেষ্টা করছি।"
        if should_recover:
            suggestion += " Recovering the desktop state first." if language == "en" else " আগে ডেস্কটপের অবস্থা ঠিক করে নিচ্ছি।"

        reflection = Reflection(
            ok=verdict in ("success", "no_action"),
            verdict=verdict,
            expected=expected,
            observed=observed,
            suggestion=suggestion,
            should_retry=should_retry,
            should_recover=should_recover,
            should_escalate=should_escalate,
            evidence=evidence,
        )
        self.stats["reflections"] += 1
        self.stats["success" if reflection.ok else "failed"] += 1
        self.stats["retries"] += int(should_retry)
        self.stats["recoveries"] += int(should_recover)
        self.stats["escalations"] += int(should_escalate)
        self.bus.emit(
            "reflection.result",
            phase=EventPhase.BRAIN,
            request_id=request.request_id,
            session_id=request.session_id,
            plan_id=plan.plan_id,
            verdict=verdict,
            ok=reflection.ok,
            should_retry=should_retry,
            should_recover=should_recover,
            should_escalate=should_escalate,
            evidence=evidence[:6],
        )
        return reflection

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.brain.reflection_enabled,
            "has_verifier": self.verifier is not None,
            **self.stats,
        }

"""Agent foundations — the shared spine every STAR 2.0 agent runs on.

Blueprint §4: *"Agents are goal-oriented workers… Tools are capabilities. The
orchestrator sequences agents."* Phase 5 introduces the first real agent (the
browser agent), so the contract lives here and every later agent (computer,
file, coding, research) inherits it instead of re-inventing it:

``OBSERVE → ACT → VERIFY → RECOVER``  with a hard step budget, dry-run
awareness, an emergency-stop check before *every* step, and one event per step
on the shared bus (``agent.started`` / ``agent.step`` / ``agent.completed`` /
``agent.failed``).

An agent never touches the OS itself. It proposes tool calls and hands them to
the Phase 4 :class:`~Backend.star.tools.executor.ToolExecutor`, which owns
validation, permissions, confirmations, audit and dry-run. That is what keeps
non-negotiable #2 true no matter how many agents get added.
"""

from __future__ import annotations

import abc
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from Backend.star.brain.schemas import AgentName
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase
from Backend.star.observability.logging import star_logger

__all__ = [
    "AgentRun",
    "AgentRunState",
    "AgentStep",
    "AgentStepPlan",
    "AgentRegistry",
    "StarAgent",
]

_log = star_logger("star2.agents")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id(prefix: str = "run") -> str:
    from agent.core.ids import new_id

    return new_id(prefix)


# ─────────────────────────────────────────────────────────────────────────────
#  Schemas
# ─────────────────────────────────────────────────────────────────────────────


class AgentRunState:
    """Why a run ended. Plain strings — they cross the gateway as JSON."""

    RUNNING = "running"                     # not terminal — only used while the loop is live
    DONE = "done"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    BLOCKED = "blocked"                     # policy/stop gate said no
    WAITING_CONFIRMATION = "waiting_confirmation"   # a human has to say yes first
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    NO_PLAN = "no_plan"                     # nothing to do for this goal

    TERMINAL = frozenset(
        {DONE, DRY_RUN, FAILED, BLOCKED, WAITING_CONFIRMATION, BUDGET_EXHAUSTED, CANCELLED, NO_PLAN}
    )


class AgentStepPlan(BaseModel):
    """One intended step, decided *before* anything runs (the dry-run preview)."""

    model_config = ConfigDict(extra="forbid")

    action: str                              # open | read | links | search | click | type | snapshot | verify
    tool: str = ""                           # registry tool name this maps to
    arguments: dict[str, Any] = Field(default_factory=dict)
    expect: str = ""                         # what "it worked" looks like — used by VERIFY
    optional: bool = False                   # a failed optional step does not fail the run
    note: str = ""


class AgentStep(BaseModel):
    """What actually happened on one step (OBSERVE/ACT/VERIFY/RECOVER output)."""

    model_config = ConfigDict(extra="forbid")

    index: int = 0
    action: str = ""
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    decision: str = ""                       # executed | simulated | denied | needs_confirmation | …
    ok: bool = False
    expected: str = ""
    observation: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    verification_note: str = ""
    recovered: bool = False
    recovery_note: str = ""
    duration_ms: float = Field(default=0.0, ge=0.0)
    error: str | None = None
    skipped: bool = False
    confirmation_id: str | None = None


class AgentRun(BaseModel):
    """The whole goal attempt — this is what the gateway returns."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: _new_run_id("run"))
    agent: AgentName = AgentName.CONVERSATION
    goal: str = ""
    state: str = AgentRunState.NO_PLAN
    dry_run: bool = True
    steps_planned: list[AgentStepPlan] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    budget: int = 6
    steps_used: int = 0
    summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    confirmation_id: str | None = None      # set when the run is waiting for a human
    session_id: str = ""
    request_id: str = ""
    plan_id: str = ""
    task_id: str = ""
    duration_ms: float = Field(default=0.0, ge=0.0)
    created_at: datetime = Field(default_factory=_utcnow)

    @property
    def succeeded(self) -> bool:
        return self.state in (AgentRunState.DONE, AgentRunState.DRY_RUN)

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["succeeded"] = self.succeeded
        return data


# ─────────────────────────────────────────────────────────────────────────────
#  The agent
# ─────────────────────────────────────────────────────────────────────────────


class StarAgent(abc.ABC):
    """Goal-oriented worker. Subclasses implement plan / act / observe / verify."""

    #: what this agent is called in plans, tasks and the gateway
    name: AgentName = AgentName.CONVERSATION
    description: str = ""
    #: registry tool names it is allowed to use (documentation + a hard filter)
    tools: tuple[str, ...] = ()
    #: words that make this agent the right choice for a goal
    keywords: tuple[str, ...] = ()
    max_steps: int = 6
    #: which kind of background workspace session this agent's artifacts land in (Phase 7)
    workspace_kind: str = "generic"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: Any = None,
        executor: Any = None,
        registry: Any = None,
        stop_gate: Callable[[], bool] | None = None,
        workspace: Any = None,
    ) -> None:
        self.settings = settings or Settings()
        self.bus = bus
        self.executor = executor
        self.registry = registry
        self._stop_gate = stop_gate
        #: optional WorkspaceManager — the invisible background desktop (blueprint §8)
        self.workspace = workspace
        self.runs: list[AgentRun] = []
        self.stats: dict[str, int] = {"runs": 0, "steps": 0, "failed_steps": 0, "recovered": 0, "blocked": 0}
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def startup(self) -> None:
        """Idempotent: the app starts an agent directly *and* via the registry."""
        if self._started:
            return None
        self._started = True
        if self.bus is not None:
            self.bus.emit(
                "agent.ready",
                phase=EventPhase.TASK,
                agent=self.name.value,
                description=self.description,
                tools=list(self.tools),
                max_steps=self.max_steps,
            )
        _log.info("star2.agent.ready agent=%s tools=%s", self.name.value, len(self.tools))

    async def aclose(self) -> None:
        self._started = False
        return None

    # ── selection ─────────────────────────────────────────────────────────
    def can_handle(self, goal: str) -> bool:
        return self.score_goal(goal) > 0.0

    def score_goal(self, goal: str) -> float:
        """0.0 = not mine. Higher wins when several agents could take the goal."""
        text = (goal or "").lower()
        hits = sum(1 for word in self.keywords if word and word in text)
        return min(1.0, hits / 2.0) if hits else 0.0

    # ── the loop ──────────────────────────────────────────────────────────
    async def run(
        self,
        goal: str,
        *,
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        task_id: str = "",
        dry_run: bool | None = None,
        max_steps: int | None = None,
    ) -> AgentRun:
        """OBSERVE → ACT → VERIFY → RECOVER, under a hard step budget."""
        started = time.monotonic()
        self.stats["runs"] += 1
        effective_dry_run = self.settings.security.dry_run if dry_run is None else bool(dry_run)
        budget = max(1, min(int(max_steps or self.max_steps), 32))
        run = AgentRun(
            agent=self.name,
            goal=goal,
            dry_run=effective_dry_run,
            budget=budget,
            session_id=session_id or self.settings.session_id,
            request_id=request_id,
            plan_id=plan_id,
            task_id=task_id,
        )
        self._emit("agent.started", run, step=None)

        if self._stopped():
            run.state = AgentRunState.BLOCKED
            run.error = "emergency stop is active — the agent will not act"
            self.stats["blocked"] += 1
            return self._finish(run, started)

        # OBSERVE (the plan is the observation: what the goal implies, step by step)
        try:
            planned = list(self.plan(goal))
        except Exception as exc:  # noqa: BLE001 — a broken plan must not escape as a traceback
            run.state = AgentRunState.FAILED
            run.error = f"planning failed: {type(exc).__name__}: {exc}"
            return self._finish(run, started)

        run.state = AgentRunState.RUNNING
        run.steps_planned = planned[:budget]
        if not planned:
            run.state = AgentRunState.NO_PLAN
            run.summary = f"nothing to do for: {goal!r}"
            return self._finish(run, started)
        if len(planned) > budget:
            run.summary = f"plan truncated from {len(planned)} steps to the budget of {budget}"

        context = self.new_context(goal, run)
        for index, step_plan in enumerate(run.steps_planned):
            if self._stopped():
                run.state = AgentRunState.BLOCKED
                run.error = "emergency stop during the run"
                self.stats["blocked"] += 1
                break

            step = AgentStep(
                index=index,
                action=step_plan.action,
                tool=step_plan.tool,
                arguments=dict(step_plan.arguments),
                expected=step_plan.expect,
            )
            step_started = time.monotonic()
            self.stats["steps"] += 1
            self._emit("agent.step", run, step=step, phase_action="act", step_plan=step_plan)

            # ACT — always through the executor, so policy/audit/dry-run apply
            outcome = await self.act(step_plan, context, run=run, dry_run=effective_dry_run)
            step.call_id = outcome.get("call_id", "")
            step.decision = str(outcome.get("decision", ""))
            step.ok = bool(outcome.get("ok", False))
            step.error = outcome.get("error")
            step.confirmation_id = outcome.get("confirmation_id")

            # OBSERVE — turn the raw result into something the next step can use
            step.observation = self.observe(step_plan, outcome, context)

            # VERIFY
            verified, note = self.verify(step_plan, step.observation, step)
            step.verified = verified
            step.verification_note = note

            if not step.ok or not verified:
                self.stats["failed_steps"] += 1
                # RECOVER — one honest attempt, never a blind retry loop
                recovered, recovery_note = await self.recover(step_plan, step, context, run=run)
                step.recovered = recovered
                step.recovery_note = recovery_note
                if recovered:
                    self.stats["recovered"] += 1
                elif not step_plan.optional:
                    step.duration_ms = (time.monotonic() - step_started) * 1000.0
                    run.steps.append(step)
                    run.steps_used = index + 1
                    refused_by_policy = step.decision in ("denied", "blocked") or bool(
                        step.observation.get("blocked_by_policy")
                    )
                    if step.decision == "needs_confirmation":
                        # not a failure: the run parks until a human decides
                        run.state = AgentRunState.WAITING_CONFIRMATION
                    elif refused_by_policy:
                        run.state = AgentRunState.BLOCKED
                    else:
                        run.state = AgentRunState.FAILED
                    run.error = step.error or note or f"step {index} ({step_plan.action}) did not verify"
                    if run.state == AgentRunState.WAITING_CONFIRMATION and step.confirmation_id:
                        run.error = f"{run.error} — approve {step.confirmation_id} to continue"
                    run.summary = f"{run.state}: {run.error[:140]}"
                    self._emit("agent.failed", run, step=step)
                    return self._finish(run, started)

            step.duration_ms = (time.monotonic() - step_started) * 1000.0
            run.steps.append(step)
            run.steps_used = index + 1
            context = self.after_step(step_plan, step, context, run)

        if run.state == AgentRunState.RUNNING:
            if len(planned) > budget:
                run.state = AgentRunState.BUDGET_EXHAUSTED
            elif effective_dry_run and self._all_simulated(run):
                run.state = AgentRunState.DRY_RUN
            else:
                run.state = AgentRunState.DONE
        run.result = self.summarise_result(run, context)
        run.summary = run.summary or self.summarise(run, context)
        return self._finish(run, started)

    def _finish(self, run: AgentRun, started: float) -> AgentRun:
        run.duration_ms = (time.monotonic() - started) * 1000.0
        run.steps_used = len(run.steps)
        if run.confirmation_id is None:
            # surface it whatever way the run ended, so a caller can approve and retry
            run.confirmation_id = next((s.confirmation_id for s in reversed(run.steps) if s.confirmation_id), None)
        self.runs.append(run)
        if len(self.runs) > 50:
            del self.runs[:-50]
        self._emit("agent.completed" if run.succeeded else "agent.failed", run, step=None)
        _log.info(
            "star2.agent.run agent=%s state=%s steps=%s dry_run=%s ms=%.1f",
            run.agent.value, run.state, run.steps_used, run.dry_run, run.duration_ms,
        )
        return run

    # ── subclass contract ─────────────────────────────────────────────────
    @abc.abstractmethod
    def plan(self, goal: str) -> Sequence[AgentStepPlan]:
        """Turn a goal into concrete, bounded steps. Must be side-effect free."""

    def new_context(self, goal: str, run: AgentRun) -> dict[str, Any]:
        """Per-run scratchpad shared between steps (urls seen, text read, …).

        Blueprint §8 puts the workspace in the flow itself — *Permission → Workspace →
        Observe* — so the session is acquired here, before the first step runs.
        """
        context: dict[str, Any] = {"goal": goal, "urls": [], "texts": [], "last_url": "", "last_text": ""}
        self.acquire_workspace(run, context)
        return context

    def acquire_workspace(self, run: AgentRun, context: dict[str, Any]) -> dict[str, Any] | None:
        """Attach (or reuse) a background session for this run. Never raises.

        The session lands in ``context["workspace"]``; a failure lands in
        ``context["workspace_error"]`` so the summary can report it honestly.
        """
        if self.workspace is None:
            return None
        try:
            session = self.workspace.acquire(
                self.workspace_kind,
                agent=self.name.value,
                label=(run.goal or "")[:60],
                dry_run=run.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — a workspace problem must not stop the run
            _log.warning("star2.agent.workspace.unavailable agent=%s error=%s", self.name.value, exc)
            context["workspace_error"] = str(exc)[:200]
            return None
        attached = {
            "session_id": session.session_id,
            "kind": session.kind.value,
            "path": str(session.root),
            "isolation": session.isolation,
            "state": session.state.value,
            "dry_run": session.dry_run,
        }
        context["workspace"] = attached
        return attached

    async def act(
        self, step_plan: AgentStepPlan, context: dict[str, Any], *, run: AgentRun, dry_run: bool
    ) -> dict[str, Any]:
        """Default ACT: call the tool through the Phase 4 executor (policy + audit)."""
        if self.executor is None:
            return {"ok": False, "decision": "unavailable", "error": "no tool executor wired to this agent"}
        result = await self.executor.call(
            step_plan.tool,
            dict(step_plan.arguments),
            session_id=run.session_id,
            request_id=run.request_id,
            plan_id=run.plan_id,
            task_id=run.task_id,
            call_id=f"{run.run_id}:{step_plan.action}",
            dry_run=dry_run,
        )
        payload = result.as_call_result()
        payload.setdefault("decision", result.decision)
        payload.setdefault("ok", result.ok)
        payload["call_id"] = result.call_id
        payload["confirmation_id"] = result.confirmation_id
        payload["_result"] = result
        return payload

    @abc.abstractmethod
    def observe(self, step_plan: AgentStepPlan, outcome: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Reduce a raw tool result to the few facts the next step needs."""

    def verify(self, step_plan: AgentStepPlan, observation: dict[str, Any], step: AgentStep) -> tuple[bool, str]:
        """Default VERIFY: the tool said ok and the decision was not a refusal."""
        if step.decision in ("denied", "blocked", "needs_confirmation"):
            return False, f"policy decision was {step.decision}"
        if not step.ok:
            return False, step.error or "tool reported failure"
        if observation.get("error"):
            return False, str(observation["error"])[:200]
        return True, "ok"

    async def recover(
        self, step_plan: AgentStepPlan, step: AgentStep, context: dict[str, Any], *, run: AgentRun
    ) -> tuple[bool, str]:
        """Default RECOVER: optional steps are dropped, everything else is not retried."""
        if step.decision == "needs_confirmation":
            return False, f"waiting for a human decision ({step.confirmation_id or 'no id'})"
        if step_plan.optional:
            return True, "optional step skipped"
        return False, "no recovery available for this step"

    def after_step(
        self, step_plan: AgentStepPlan, step: AgentStep, context: dict[str, Any], run: AgentRun
    ) -> dict[str, Any]:
        return context

    def summarise(self, run: AgentRun, context: dict[str, Any]) -> str:
        done = sum(1 for s in run.steps if s.ok)
        return f"{done}/{len(run.steps)} step(s) succeeded ({run.state})"

    def summarise_result(self, run: AgentRun, context: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"steps": run.steps_used, "state": run.state}
        workspace = context.get("workspace")
        if workspace:
            result["workspace"] = workspace
        if context.get("workspace_error"):
            result["workspace_error"] = context["workspace_error"]
        return result

    # ── helpers ───────────────────────────────────────────────────────────
    def _stopped(self) -> bool:
        return bool(self._stop_gate()) if callable(self._stop_gate) else False

    @staticmethod
    def _all_simulated(run: AgentRun) -> bool:
        decisions = [step.decision for step in run.steps]
        return bool(decisions) and all(d in ("simulated", "") for d in decisions)

    def _emit(self, kind: str, run: AgentRun, *, step: AgentStep | None, **extra: Any) -> None:
        if self.bus is None:
            return
        payload: dict[str, Any] = {
            "agent": run.agent.value,
            "run_id": run.run_id,
            "goal": run.goal[:200],
            "state": run.state,
            "dry_run": run.dry_run,
            "steps_used": run.steps_used,
        }
        if step is not None:
            payload.update(
                {
                    "step_index": step.index,
                    "action": step.action,
                    "tool": step.tool,
                    "decision": step.decision,
                    "ok": step.ok,
                    "verified": step.verified,
                    "recovered": step.recovered,
                }
            )
        payload.update({k: v for k, v in extra.items() if not k.startswith("_") and not isinstance(v, BaseModel)})
        self.bus.emit(kind, phase=EventPhase.TASK, session_id=run.session_id or self.settings.session_id, **payload)

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "description": self.description,
            "tools": list(self.tools),
            "keywords": list(self.keywords),
            "max_steps": self.max_steps,
            "executor": self.executor is not None,
            "started": self._started,
            "runs": len(self.runs),
            "stats": dict(self.stats),
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if self.executor is None:
            problems.append("no executor wired — the agent cannot act")
        if self.settings.security.dry_run is False:
            problems.append("dry-run is off — real actions are permitted")
        if self.stats["runs"] and self.stats["failed_steps"] > self.stats["steps"]:
            problems.append("every step failed")
        return {"status": "degraded" if problems else "ok", "detail": {"problems": problems, **self.stats}}

    def last_run(self) -> AgentRun | None:
        return self.runs[-1] if self.runs else None


# ─────────────────────────────────────────────────────────────────────────────
#  Registry
# ─────────────────────────────────────────────────────────────────────────────


class AgentRegistry:
    """The orchestrator's (Phase 10) view of every worker that exists."""

    def __init__(self, agents: Iterable[StarAgent] | None = None) -> None:
        self._agents: dict[str, StarAgent] = {}
        for agent in agents or ():
            self.register(agent)

    def register(self, agent: StarAgent, *, replace: bool = False) -> StarAgent:
        key = agent.name.value
        if key in self._agents and not replace:
            raise ValueError(f"agent '{key}' is already registered (pass replace=True)")
        self._agents[key] = agent
        return agent

    def get(self, name: str | AgentName) -> StarAgent | None:
        key = name.value if isinstance(name, AgentName) else str(name)
        return self._agents.get(key)

    def names(self) -> list[str]:
        return sorted(self._agents)

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, item: object) -> bool:
        key = item.value if isinstance(item, AgentName) else str(item)
        return key in self._agents

    def dispatch(self, goal: str) -> StarAgent | None:
        """Highest scoring agent that can handle the goal (``None`` = conversation)."""
        best: tuple[float, StarAgent] | None = None
        for agent in self._agents.values():
            score = agent.score_goal(goal)
            if score > 0 and (best is None or score > best[0]):
                best = (score, agent)
        return best[1] if best else None

    async def startup(self) -> None:
        for agent in self._agents.values():
            await agent.startup()

    async def aclose(self) -> None:
        for agent in reversed(list(self._agents.values())):
            try:
                await agent.aclose()
            except Exception as exc:  # noqa: BLE001 — shutdown must complete
                _log.warning("star2.agent.close_failed agent=%s error=%s", agent.name.value, exc)

    def describe(self) -> list[dict[str, Any]]:
        return [agent.describe() for agent in self._agents.values()]

    def runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for agent in self._agents.values():
            out.extend(run.public() for run in agent.runs[-limit:])
        out.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return out[:limit]

    def health(self) -> dict[str, Any]:
        checks = {name: agent.health() for name, agent in self._agents.items()}
        statuses = [str(value.get("status", "ok")) for value in checks.values()]
        overall = "fail" if "fail" in statuses else "degraded" if "degraded" in statuses else "ok"
        return {"status": overall if checks else "pending", "detail": {"agents": len(checks), "checks": checks}}

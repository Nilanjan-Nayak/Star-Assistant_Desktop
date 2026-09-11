"""ReActAgent — the orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from agent.core.enums import AgentState
from agent.core.errors import AgentError, SafetyViolation, StateTransitionError
from agent.core.events import BUS, EpisodeEndedEvent, EpisodeStartedEvent
from agent.core.ids import new_episode_id
from agent.core.logging import get_logger, log_ctx
from agent.core.metrics import METRICS
from agent.motor.controller import MotorController
from agent.motor.spec import spec_from_kind
from agent.planning.episode import Episode
from agent.core.enums import MemoryKind
from agent.memory.store import MemoryStore
from agent.planning.journal import ActionJournal
from agent.planning.memory import EpisodicMemory
from agent.planning.planner import Planner
from agent.planning.planners.stub import StubPlanner
from agent.planning.state_machine import StateMachine
from agent.planning.step import PlanStep
from agent.safety.governor import SafetyGovernor
from agent.skills.registry import SkillRegistry
from agent.world.model import WorldModel

_log = get_logger("agent.react")


class ReActAgent:
    def __init__(
        self,
        registry: SkillRegistry,
        motor: MotorController,
        world: WorldModel,
        governor: SafetyGovernor,
        planner: Planner | None = None,
        memory: EpisodicMemory | None = None,
        semantic: MemoryStore | None = None,
        journal: ActionJournal | None = None,
        max_steps: int = 20,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.registry = registry
        self.motor = motor
        self.world = world
        self.governor = governor
        self.semantic = semantic
        self.planner = planner or StubPlanner(semantic=semantic)
        self.memory = memory or EpisodicMemory()
        self.journal = journal or ActionJournal()
        self.max_steps = max_steps
        self.fsm = StateMachine()

    async def run(self, goal: str) -> Episode:
        if not goal.strip():
            raise ValueError("goal must be non-empty")
        episode = Episode(episode_id=new_episode_id(), goal=goal)
        self.governor.begin_episode()
        if self.semantic is not None:
            self.semantic.learn_from_goal(goal, episode_id=episode.episode_id)
        await BUS.publish(EpisodeStartedEvent(episode_id=episode.episode_id, goal=goal))

        try:
            await self.fsm.transition(AgentState.PERCEIVING)
            log_ctx(
                _log,
                logging.INFO,
                "episode.start",
                episode_id=episode.episode_id,
                goal=goal,
            )

            for step_num in range(1, self.max_steps + 1):
                if self.fsm.state is not AgentState.PERCEIVING:
                    await self.fsm.transition(AgentState.PERCEIVING)
                try:
                    self.world.observe()
                except AgentError as exc:
                    log_ctx(_log, logging.WARNING, "observe.failed", error=str(exc))

                await self.fsm.transition(AgentState.PLANNING)
                step = await self.planner.next_step(
                    goal, episode.steps, self.world, self.memory
                )
                if step is None:
                    await self.fsm.transition(AgentState.SUCCEEDED)
                    episode.final_state = AgentState.SUCCEEDED
                    break

                log_ctx(
                    _log,
                    logging.INFO,
                    "step.plan",
                    step=step_num,
                    thought=step.thought,
                    skill=step.skill,
                    raw=step.raw_action,
                )

                await self.fsm.transition(AgentState.ACTING)
                try:
                    step = await self._act(step)
                except SafetyViolation as sv:
                    step.observation = f"BLOCKED: {sv}"
                    step.success = False
                    episode.steps.append(step)
                    await self.fsm.transition(AgentState.BLOCKED)
                    episode.final_state = AgentState.BLOCKED
                    break
                except AgentError as ae:
                    step.observation = f"ERROR: {ae}"
                    step.success = False

                episode.steps.append(step)
                await self.fsm.transition(AgentState.VERIFYING)
                await self.fsm.transition(AgentState.REFLECTING)
                log_ctx(
                    _log,
                    logging.INFO if step.success else logging.WARNING,
                    "step.done",
                    step=step_num,
                    ok=step.success,
                )
            else:
                await self.fsm.transition(AgentState.FAILED)
                episode.final_state = AgentState.FAILED

        except StateTransitionError:
            if self.fsm.can_transition(AgentState.ABORTED):
                await self.fsm.transition(AgentState.ABORTED)
            episode.final_state = AgentState.ABORTED
            raise
        finally:
            episode.ended_at = datetime.now(timezone.utc)
            self.memory.save(episode)
            self.governor.dump_audit()
            METRICS.inc("episode.completed", state=episode.final_state.value)
            await BUS.publish(
                EpisodeEndedEvent(
                    episode_id=episode.episode_id,
                    state=episode.final_state,
                    steps=len(episode.steps),
                )
            )
            log_ctx(
                _log,
                logging.INFO,
                "episode.end",
                episode_id=episode.episode_id,
                state=episode.final_state.value,
                steps=len(episode.steps),
            )
        return episode

    async def _act(self, step: PlanStep) -> PlanStep:
        if step.skill is not None:
            res = await self.registry.run(step.skill, dict(step.params))
            step.observation = res.model_dump_json()
            step.success = res.ok
            return step
        if step.raw_action is not None:
            spec = spec_from_kind(step.raw_action, dict(step.params))
            await self.governor.check(spec)
            motor_res = await self.motor.execute(spec)
            self.governor.log(motor_res)
            self.journal.record(spec, motor_res)
            step.observation = motor_res.model_dump_json()
            step.success = motor_res.success
            return step
        step.observation = "no skill or raw_action"
        step.success = False
        return step

    def _imprint(self, episode: Episode) -> None:
        store = self.semantic
        if store is None:
            return
        store.remember(
            (
                f"Episode {episode.episode_id}: goal={episode.goal!r} "
                f"state={episode.final_state.value} steps={len(episode.steps)}"
            ),
            kind=MemoryKind.EPISODE,
            source_episode=episode.episode_id,
        )
        if episode.final_state is not AgentState.SUCCEEDED:
            return
        for step in episode.steps:
            if step.success and step.skill is not None:
                store.learn_from_skill(
                    step.skill, dict(step.params), episode_id=episode.episode_id
                )

"""Phase 10 — advanced orchestration tests.

Verifies:
* OrchestratorSettings: defaults, env overrides, validation
* PlanCheckpoint & CheckpointStore: save, reload, prune, indexing, events
* PlanRouter: priority sequencing, agent routing, dispatch fallback, tool routing
* RecoveryManager: bounded retries, safety block refusal, honest RECOVERED state
* TaskRunner: multi-agent execution, cancel, cancel_all, stop gate, dry-run
* Orchestrator engine: execute(plan), pause, resume, rollback, ledgers
* Orchestrator tools: task_cancel, task_status, plan_pause, plan_resume, plan_checkpoint, orchestrator_status
* StarApplication integration: capabilities, health, app.tasks/plans, app.cancel_task, app.emergency_stop
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from Backend.star.agents.base import (
    AgentRegistry,
    AgentRun,
    AgentRunState,
    AgentStepPlan,
    StarAgent,
)
from Backend.star.brain.schemas import (
    AgentName,
    Context,
    Plan,
    Task,
    TaskState,
    ToolCall,
    ToolCallState,
    Verification,
)
from Backend.star.config.settings import (
    OrchestratorSettings,
    PathsSettings,
    SecuritySettings,
    Settings,
)
from Backend.star.main import build_application
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.orchestrator import (
    CheckpointStatus,
    CheckpointStore,
    Orchestrator,
    PlanCheckpoint,
    PlanRouter,
    RecoveryManager,
    TaskRoute,
    TaskRunner,
    build_orchestrator,
    register_orchestrator_tools,
)
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.registry import StarToolRegistry, ToolSpec


# ── Test Fixtures ─────────────────────────────────────────────────────────────

class DummyAgent(StarAgent):
    """Simple test agent implementing StarAgent contract."""

    name = AgentName.BROWSER
    description = "Test browser worker"
    tools = ("browser_open", "browser_read")
    keywords = ("browser", "web", "url")

    def plan(self, goal: str) -> list[AgentStepPlan]:
        return [
            AgentStepPlan(action="navigate", tool="browser_open", arguments={"url": "https://example.com"}),
            AgentStepPlan(action="extract", tool="browser_read", arguments={"selector": "body"}),
        ]

    def observe(self, step_plan: AgentStepPlan, outcome: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"ok": bool(outcome.get("ok", True))}

    async def act(self, step_plan: AgentStepPlan, context: dict[str, Any], *, run: AgentRun, dry_run: bool) -> dict[str, Any]:
        return {"ok": True, "decision": "simulated" if dry_run else "done", "result": {"success": True}}


class FailingAgent(StarAgent):
    """Agent designed to fail for recovery testing."""

    name = AgentName.COMPUTER
    description = "Failing computer worker"
    tools = ("screen_click",)
    keywords = ("click", "mouse")

    def plan(self, goal: str) -> list[AgentStepPlan]:
        return [AgentStepPlan(action="click", tool="screen_click", arguments={"x": 10, "y": 20})]

    def observe(self, step_plan: AgentStepPlan, outcome: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": outcome.get("error")}

    async def act(self, step_plan: AgentStepPlan, context: dict[str, Any], *, run: AgentRun, dry_run: bool) -> dict[str, Any]:
        return {"ok": False, "error": "device disconnected", "decision": "failed"}


@pytest.fixture
def temp_paths(tmp_path: Path) -> PathsSettings:
    paths = PathsSettings(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspace",
        logs_dir=tmp_path / "logs",
        memory_db=tmp_path / "memory.db",
        episodes_path=tmp_path / "episodes.jsonl",
        patterns_path=tmp_path / "patterns.jsonl",
        candidates_path=tmp_path / "candidates.jsonl",
        feedback_path=tmp_path / "feedback.jsonl",
        checkpoints_path=tmp_path / "orchestrator" / "checkpoints.jsonl",
        browser_profile_root=tmp_path / "profiles",
    )
    paths.ensure()
    return paths


@pytest.fixture
def bus() -> StarEventBus:
    return StarEventBus()


@pytest.fixture
def app_settings(temp_paths: PathsSettings) -> Settings:
    return Settings(
        paths=temp_paths,
        security=SecuritySettings(dry_run=True),
        orchestrator=OrchestratorSettings(enabled=True, max_retries=2),
    )


# ── Settings Tests ────────────────────────────────────────────────────────────

def test_orchestrator_settings_defaults() -> None:
    cfg = OrchestratorSettings()
    assert cfg.enabled is True
    assert cfg.max_parallel == 4
    assert cfg.max_retries == 2
    assert cfg.task_timeout_s == 30.0
    assert cfg.plan_timeout_s == 120.0
    assert cfg.checkpoint_enabled is True
    assert cfg.max_checkpoints == 20
    assert cfg.allow_parallel_agents is True


def test_orchestrator_settings_env_override(monkeypatch: pytest.MonkeyPatch, temp_paths: PathsSettings) -> None:
    monkeypatch.setenv("STAR_ORCHESTRATOR_ENABLED", "false")
    monkeypatch.setenv("STAR_ORCHESTRATOR_MAX_PARALLEL", "8")
    monkeypatch.setenv("STAR_ORCHESTRATOR_MAX_RETRIES", "4")
    monkeypatch.setenv("STAR_ORCHESTRATOR_TASK_TIMEOUT_S", "45.0")
    monkeypatch.setenv("STAR_ORCHESTRATOR_CHECKPOINTS", "false")

    cfg = Settings.from_env(load_env_file=False)
    assert cfg.orchestrator.enabled is False
    assert cfg.orchestrator.max_parallel == 8
    assert cfg.orchestrator.max_retries == 4
    assert cfg.orchestrator.task_timeout_s == 45.0
    assert cfg.orchestrator.checkpoint_enabled is False


# ── CheckpointStore Tests ─────────────────────────────────────────────────────

def test_checkpoint_store_save_and_retrieve(temp_paths: PathsSettings, bus: StarEventBus) -> None:
    store = CheckpointStore(temp_paths.checkpoints_path, bus=bus)
    chk = PlanCheckpoint(
        plan_id="plan_001",
        label="initial",
        status=CheckpointStatus.ACTIVE,
        task_states={"t1": TaskState.DONE, "t2": TaskState.PENDING},
        completed_tasks=["t1"],
        pending_tasks=["t2"],
    )
    saved = store.save(chk)
    assert saved.checkpoint_id == chk.checkpoint_id
    assert store.count() == 1

    # Retrieve by id
    fetched = store.get(chk.checkpoint_id)
    assert fetched is not None
    assert fetched.plan_id == "plan_001"
    assert fetched.task_states["t1"] == TaskState.DONE

    # Latest for plan
    latest = store.latest("plan_001")
    assert latest is not None
    assert latest.checkpoint_id == chk.checkpoint_id

    # Event emitted
    events = bus.history(kinds=frozenset({"plan.checkpoint"}))
    assert len(events) == 1
    assert events[0].payload["plan_id"] == "plan_001"
    assert len(events) == 1
    assert events[0].payload["plan_id"] == "plan_001"


def test_checkpoint_store_persistence(temp_paths: PathsSettings) -> None:
    store1 = CheckpointStore(temp_paths.checkpoints_path)
    chk1 = PlanCheckpoint(plan_id="p1", label="c1")
    chk2 = PlanCheckpoint(plan_id="p1", label="c2")
    store1.save(chk1)
    store1.save(chk2)
    assert store1.count() == 2

    # Open fresh store from same path
    store2 = CheckpointStore(temp_paths.checkpoints_path)
    assert store2.count() == 2
    assert store2.latest("p1").checkpoint_id == chk2.checkpoint_id
    assert len(store2.list_for_plan("p1")) == 2


def test_checkpoint_store_prune(temp_paths: PathsSettings) -> None:
    store = CheckpointStore(temp_paths.checkpoints_path, max_records=3)
    for i in range(5):
        store.save(PlanCheckpoint(plan_id=f"p{i}", label=f"c{i}"))

    assert store.count() == 5
    removed = store.prune(max_records=3)
    assert removed == 2
    assert store.count() == 3


# ── PlanRouter Tests ──────────────────────────────────────────────────────────

def test_router_priority_sequencing() -> None:
    router = PlanRouter()
    t1 = Task(task_id="t1", goal="secondary task", priority=5)
    t2 = Task(task_id="t2", goal="urgent task", priority=1)
    t3 = Task(task_id="t3", goal="normal task", priority=3)
    plan = Plan(plan_id="p1", tasks=[t1, t2, t3])

    seq = router.plan_sequence(plan)
    assert [t.task_id for t in seq] == ["t2", "t3", "t1"]


def test_router_routing_conversation_and_agents(app_settings: Settings) -> None:
    dummy_browser = DummyAgent(app_settings)
    registry = AgentRegistry([dummy_browser])
    router = PlanRouter(registry=registry)

    # Conversation task
    conv_task = Task(task_id="tc", goal="hello", agent=AgentName.CONVERSATION)
    route_conv = router.route_task(conv_task)
    assert route_conv.worker_type == "conversation"

    # Registered agent match
    browser_task = Task(task_id="tb", goal="open website", agent=AgentName.BROWSER)
    route_browser = router.route_task(browser_task)
    assert route_browser.worker_type == "agent"
    assert route_browser.worker_name == "browser"

    # Goal dispatch fallback
    dispatch_task = Task(task_id="td", goal="search url website", agent=AgentName.SYSTEM)
    route_dispatch = router.route_task(dispatch_task)
    assert route_dispatch.worker_type == "agent"
    assert route_dispatch.worker_name == "browser"


def test_router_routing_tool_calls(app_settings: Settings) -> None:
    executor = MagicMock(spec=ToolExecutor)
    router = PlanRouter(executor=executor)

    tool_task = Task(
        task_id="tt",
        goal="execute tool",
        agent=AgentName.FILESYSTEM,
        steps=[ToolCall(tool="file_read", arguments={"path": "test.txt"})],
    )
    route_tool = router.route_task(tool_task)
    assert route_tool.worker_type == "executor"
    assert route_tool.worker_name == "tool_executor"


# ── RecoveryManager Tests ─────────────────────────────────────────────────────

def test_recovery_manager_eligibility(app_settings: Settings) -> None:
    rec = RecoveryManager(app_settings)
    task = Task(task_id="t1", goal="test", max_retries=2, retries=0)
    assert rec.can_retry(task) is True

    task.retries = 2
    assert rec.can_retry(task) is False

    # Blocked / waiting / cancelled states are NEVER retried
    task.retries = 0
    task.state = TaskState.BLOCKED
    assert rec.can_retry(task) is False

    task.state = TaskState.WAITING_CONFIRMATION
    assert rec.can_retry(task) is False

    task.state = TaskState.CANCELLED
    assert rec.can_retry(task) is False


def test_recovery_manager_detects_safety_refusal(app_settings: Settings) -> None:
    rec = RecoveryManager(app_settings)
    task = Task(task_id="t1", goal="dangerous operation")

    assert rec.is_safety_refusal(task, "denied by policy") is True
    assert rec.is_safety_refusal(task, "emergency stop active") is True
    assert rec.is_safety_refusal(task, "guardrail violation") is True
    assert rec.is_safety_refusal(task, "timeout") is False


@pytest.mark.asyncio
async def test_recovery_manager_successful_retry(app_settings: Settings, bus: StarEventBus) -> None:
    rec = RecoveryManager(app_settings, bus=bus)

    task = Task(task_id="t1", goal="transient network call", max_retries=2)
    attempts = 0

    async def _action() -> bool:
        nonlocal attempts
        attempts += 1
        return attempts >= 2  # succeeds on 2nd attempt

    ok = await rec.attempt_recovery(task, _action, session_id="s1")
    assert ok is True
    assert task.state is TaskState.RECOVERED
    events = bus.history(kinds=frozenset({"task.recovered"}))
    assert len(events) == 1
    assert events[0].payload["task_id"] == "t1"


@pytest.mark.asyncio
async def test_recovery_manager_exhausted_retries(app_settings: Settings, bus: StarEventBus) -> None:
    rec = RecoveryManager(app_settings, bus=bus)

    task = Task(task_id="t1", goal="permanently failing call", max_retries=2)

    async def _action() -> bool:
        return False

    ok = await rec.attempt_recovery(task, _action, session_id="s1")
    assert ok is False
    assert task.state is TaskState.FAILED
    events = bus.history(kinds=frozenset({"task.failed"}))
    assert len(events) == 1


# ── TaskRunner Tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_runner_executes_conversation(app_settings: Settings, bus: StarEventBus) -> None:
    runner = TaskRunner(app_settings, bus=bus)
    task = Task(task_id="tc", goal="reply to user", agent=AgentName.CONVERSATION, summary="Hello friend")
    route = TaskRoute(task_id="tc", agent=AgentName.CONVERSATION, worker_type="conversation")

    res = await runner.run_task(task, route)
    assert res.state is TaskState.DONE
    assert res.started_at is not None
    assert res.ended_at is not None
    started_events = bus.history(kinds=frozenset({"task.started"}))
    completed_events = bus.history(kinds=frozenset({"task.completed"}))
    assert len(started_events) == 1
    assert len(completed_events) == 1


@pytest.mark.asyncio
async def test_task_runner_executes_agent_dry_run(app_settings: Settings, bus: StarEventBus) -> None:
    dummy_browser = DummyAgent(app_settings)
    registry = AgentRegistry([dummy_browser])
    runner = TaskRunner(app_settings, bus=bus, registry=registry)

    task = Task(task_id="tb", goal="visit documentation", agent=AgentName.BROWSER)
    route = TaskRoute(task_id="tb", agent=AgentName.BROWSER, worker_type="agent", worker_name="browser")

    res = await runner.run_task(task, route, dry_run=True)
    assert res.state is TaskState.DONE
    assert res.verification.ok is True
    assert "dry-run: simulated" in res.verification.note
    assert "run_id" in res.checkpoint


@pytest.mark.asyncio
async def test_task_runner_cancellation(app_settings: Settings, bus: StarEventBus) -> None:
    runner = TaskRunner(app_settings, bus=bus)
    task = Task(task_id="tc", goal="long running task", agent=AgentName.SYSTEM)
    runner._task_records[task.task_id] = task

    res = await runner.cancel("tc")
    assert res["ok"] is True
    assert res["cancelled"] is True
    assert task.state is TaskState.CANCELLED


@pytest.mark.asyncio
async def test_task_runner_stop_gate_blocks_execution(app_settings: Settings) -> None:
    stopped = True
    runner = TaskRunner(app_settings, stop_gate=lambda: stopped)
    task = Task(task_id="t1", goal="action while stopped", agent=AgentName.SYSTEM)
    route = TaskRoute(task_id="t1", agent=AgentName.SYSTEM, worker_type="conversation")

    res = await runner.run_task(task, route)
    assert res.state is TaskState.BLOCKED
    assert "emergency stop is active" in res.error


# ── Orchestrator Engine Tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_execute_plan(app_settings: Settings, bus: StarEventBus) -> None:
    dummy_browser = DummyAgent(app_settings)
    registry = AgentRegistry([dummy_browser])
    orch = build_orchestrator(app_settings, bus=bus, registry=registry)

    t1 = Task(task_id="t1", goal="say greeting", agent=AgentName.CONVERSATION, priority=1)
    t2 = Task(task_id="t2", goal="read website", agent=AgentName.BROWSER, priority=2)
    plan = Plan(plan_id="p_test", intent="research", tasks=[t1, t2])

    executed_plan = await orch.execute(plan, dry_run=True)
    assert executed_plan.plan_id == "p_test"
    assert t1.state is TaskState.DONE
    assert t2.state is TaskState.DONE
    events = bus.history(kinds=frozenset({"plan.executed"}))
    assert len(events) == 1
    assert events[0].payload["executed"] == 2

    # Check ledgers
    assert len(orch.tasks()) == 2
    assert len(orch.plans()) == 1
    assert orch.get_plan("p_test") is not None


@pytest.mark.asyncio
async def test_orchestrator_pause_and_resume(app_settings: Settings, bus: StarEventBus) -> None:
    orch = build_orchestrator(app_settings, bus=bus)
    t1 = Task(task_id="t1", goal="step 1", agent=AgentName.CONVERSATION)
    t2 = Task(task_id="t2", goal="step 2", agent=AgentName.CONVERSATION)
    plan = Plan(plan_id="p_pause", tasks=[t1, t2])

    # Pause plan
    pause_res = orch.pause_plan("p_pause")
    assert pause_res["ok"] is True
    assert pause_res["paused"] is True
    assert "p_pause" in orch._paused_plans

    # Resume plan
    resume_res = await orch.resume_plan("p_pause", plan)
    assert resume_res["ok"] is True
    assert resume_res["resumed"] is True
    assert "p_pause" not in orch._paused_plans


@pytest.mark.asyncio
async def test_orchestrator_rollback(app_settings: Settings, bus: StarEventBus) -> None:
    orch = build_orchestrator(app_settings, bus=bus)
    t1 = Task(task_id="t1", goal="step 1", agent=AgentName.CONVERSATION, state=TaskState.DONE)
    plan = Plan(plan_id="p_rb", tasks=[t1])
    orch._active_plans["p_rb"] = plan

    chk = orch._create_checkpoint(plan, label="snapshot_1")
    # Mutate task
    t1.state = TaskState.FAILED

    rb_res = orch.rollback_plan("p_rb", chk.checkpoint_id)
    assert rb_res["ok"] is True
    assert t1.state is TaskState.DONE


# ── Orchestrator Tools Tests ──────────────────────────────────────────────────

def test_orchestrator_tools_registration(app_settings: Settings) -> None:
    registry = StarToolRegistry()
    orch = build_orchestrator(app_settings)
    specs = register_orchestrator_tools(registry, app_settings, orchestrator=orch)

    assert len(specs) == 6
    names = {s.name for s in specs}
    assert "task_cancel" in names
    assert "task_status" in names
    assert "plan_pause" in names
    assert "plan_resume" in names
    assert "plan_checkpoint" in names
    assert "orchestrator_status" in names


def test_orchestrator_status_tool(app_settings: Settings) -> None:
    registry = StarToolRegistry()
    orch = build_orchestrator(app_settings)
    register_orchestrator_tools(registry, app_settings, orchestrator=orch)

    status_tool = registry.get("orchestrator_status")
    assert status_tool is not None
    res = status_tool.handler()
    assert res["ok"] is True
    assert res["name"] == "orchestrator"
    assert "active_plans" in res


# ── StarApplication Integration Tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_application_orchestrator_integration(app_settings: Settings) -> None:
    app = build_application(app_settings)
    await app.startup()
    try:
        assert "orchestrator" in app.capabilities()

        health = app.health()
        assert health["ok"] is True
        assert health["checks"]["orchestrator"]["status"] == "ok"

        # Tasks and plans ledger
        assert isinstance(app.tasks(), list)
        assert isinstance(app.plans(), list)

        # Cancel unknown task returns graceful error
        res = await app.cancel_task("nonexistent")
        assert res["ok"] is False

        # Emergency stop cancels orchestrator
        stop_res = await app.emergency_stop(reason="test")
        assert stop_res["ok"] is True
        assert "cancelled_tasks" in stop_res
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_application_chat_executes_through_orchestrator(app_settings: Settings) -> None:
    app = build_application(app_settings)
    await app.startup()
    try:
        # A simple greeting chat
        res = await app.chat("hello", session_id="s_test")
        assert res["ok"] is True
        assert len(app.plans()) >= 1
        assert len(app.tasks()) >= 1
    finally:
        await app.aclose()

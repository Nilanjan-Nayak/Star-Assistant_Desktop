"""Phase 6 — computer agent tests: guardrails, motor stack, tools, agent, wiring.

Headless by construction: the motor runs on ``NullBackend`` + ``FakeCapture``
(both from the existing ``agent/`` layer), so these tests record intended actions
without needing a display, a mouse or pyautogui.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agent.core.enums import ActionKind
from agent.motor.spec import spec_from_kind

from Backend.star.computer.agent import ComputerAgent, build_computer
from Backend.star.computer.guardrails import DESTRUCTIVE_TEXT, ComputerGuardrails
from Backend.star.computer.motor import MotorStack, build_motor, describe_action
from Backend.star.computer.tools import ComputerToolkit, register_computer_tools
from Backend.star.config.settings import ComputerSettings, PathsSettings, SecuritySettings, Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.permissions import PermissionEngine
from Backend.star.tools.registry import StarToolRegistry


def _computer_kwargs(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Accept a bare string for the tuple-typed fields (mirrors env parsing)."""
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        is_tuple = getattr(ComputerSettings.model_fields[key].annotation, "__origin__", None) is tuple
        out[key] = (value,) if (is_tuple and isinstance(value, str)) else value
    return out


def _settings(tmp_path: Path, *, dry_run: bool = True, computer: dict[str, Any] | None = None, **security: Any) -> Settings:
    return Settings(
        paths=PathsSettings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            workspace_root=tmp_path / "workspace",
            memory_db=tmp_path / "memory.db",
            patterns_path=tmp_path / "patterns.jsonl",
            episodes_path=tmp_path / "episodes.jsonl",
        ),
        security=SecuritySettings(dry_run=dry_run, audit_path=str(tmp_path / "audit.jsonl"), **security),
        computer=ComputerSettings(**_computer_kwargs(computer)),
    )


def _stack(tmp_path: Path, settings: Settings, *, bus: StarEventBus | None = None):
    bus = bus or StarEventBus()
    registry = StarToolRegistry(settings, bus=bus, import_legacy=False)
    toolkit = register_computer_tools(registry, settings, bus=bus)
    audit = AuditLog(settings.security.audit_path, bus=bus)
    confirmations = ConfirmationStore(settings, bus=bus)
    permissions = PermissionEngine(settings, bus=bus, confirmations=confirmations)
    executor = ToolExecutor(
        settings, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=bus
    )
    agent = ComputerAgent(settings, bus=bus, executor=executor, registry=registry, toolkit=toolkit)
    return agent, executor, registry, toolkit, audit, bus, confirmations


# ── guardrails ────────────────────────────────────────────────────────────────


def test_guardrails_allow_in_bounds_actions(tmp_path) -> None:
    guard = ComputerGuardrails(_settings(tmp_path))
    assert guard.check(spec_from_kind(ActionKind.CLICK, {"x": 480, "y": 320})).ok is True
    assert guard.check(spec_from_kind(ActionKind.MOVE, {"x": 0, "y": 0})).ok is True
    assert guard.check(spec_from_kind(ActionKind.SCREENSHOT, {})).ok is True
    assert guard.stats["allowed"] == 3


def test_guardrails_refuse_off_screen_coordinates(tmp_path) -> None:
    guard = ComputerGuardrails(_settings(tmp_path, computer={"screen_width": 1920, "screen_height": 1080}))
    verdict = guard.check(spec_from_kind(ActionKind.CLICK, {"x": 4000, "y": 3000}))
    assert verdict.ok is False and verdict.rule == "bounds" and verdict.risk == "high"
    assert "1920x1080" in verdict.reason


def test_guardrails_refuse_forbidden_regions(tmp_path) -> None:
    guard = ComputerGuardrails(_settings(tmp_path, computer={"forbidden_regions": ("100,100,200,120")}))
    inside = guard.check(spec_from_kind(ActionKind.CLICK, {"x": 150, "y": 150}))
    assert inside.ok is False and inside.rule == "forbidden_region" and inside.risk == "critical"
    assert guard.check(spec_from_kind(ActionKind.CLICK, {"x": 500, "y": 500})).ok is True
    assert guard.governor_config().forbidden_regions[0].width == 200


def test_guardrails_refuse_destructive_and_oversized_text(tmp_path) -> None:
    guard = ComputerGuardrails(_settings(tmp_path, computer={"max_text_chars": 200}))
    typed = guard.check(spec_from_kind(ActionKind.TYPE, {"text": "sudo rm -rf / and walk away"}))
    assert typed.ok is False and typed.rule == "destructive_text" and typed.risk == "critical"
    assert any(needle in "rm -rf /" for needle in DESTRUCTIVE_TEXT)

    long_text = guard.check(spec_from_kind(ActionKind.TYPE, {"text": "a" * 201}))
    assert long_text.ok is False and long_text.rule == "text_length"
    assert guard.check(spec_from_kind(ActionKind.TYPE, {"text": "hello star"})).ok is True


def test_guardrails_block_dangerous_hotkeys_but_allow_alt_tab(tmp_path) -> None:
    guard = ComputerGuardrails(_settings(tmp_path))
    blocked = guard.check(spec_from_kind(ActionKind.HOTKEY, {"keys": ["ctrl", "alt", "del"]}))
    assert blocked.ok is False and blocked.rule == "blocked_hotkey" and blocked.risk == "critical"
    assert guard.check(spec_from_kind(ActionKind.HOTKEY, {"keys": ["alt", "tab"]})).ok is True
    assert guard.check(spec_from_kind(ActionKind.PRESS, {"key": "enter"})).ok is True


def test_guardrails_cap_scroll_and_wait(tmp_path) -> None:
    guard = ComputerGuardrails(_settings(tmp_path, computer={"max_scroll": 10, "max_wait_s": 5.0}))
    assert guard.check(spec_from_kind(ActionKind.SCROLL, {"amount": 99})).rule == "scroll"
    assert guard.check(spec_from_kind(ActionKind.SCROLL, {"amount": 3})).ok is True
    assert guard.check(spec_from_kind(ActionKind.WAIT, {"duration": 30.0})).rule == "wait"
    assert guard.check(spec_from_kind(ActionKind.WAIT, {"duration": 2.0})).ok is True


async def test_guardrails_run_the_existing_safety_governor(tmp_path) -> None:
    """The six invariants are reused, not reimplemented — and they bite."""
    settings = _settings(tmp_path, max_actions_per_session=2)
    guard = ComputerGuardrails(settings)
    assert set(guard.describe()["governor_invariants"]) >= {
        "total_budget", "rate_limit", "wall_clock", "forbidden_region", "keyword_blocklist", "capability",
    }
    spec = spec_from_kind(ActionKind.MOVE, {"x": 5, "y": 5})
    assert (await guard.authorise(spec)).ok is True
    assert (await guard.authorise(spec)).ok is True
    third = await guard.authorise(spec)
    assert third.ok is False and third.rule == "governor" and third.governor_rule == "BudgetExhausted"
    assert guard.stats["governor_refused"] == 1


def test_guardrails_emit_safety_blocked_and_report_health(tmp_path) -> None:
    bus = StarEventBus()
    guard = ComputerGuardrails(_settings(tmp_path), bus=bus)
    guard.check(spec_from_kind(ActionKind.CLICK, {"x": 9999, "y": 9999}))
    blocked = [event for event in bus.history(limit=20) if event.kind == "safety.blocked"]
    assert blocked and blocked[0].payload["layer"] == "computer_guardrails"
    assert blocked[0].payload["rule"] == "bounds"
    assert guard.health()["status"] == "ok", "dry-run + null backend is a healthy posture"
    live = ComputerGuardrails(_settings(tmp_path, dry_run=False, computer={"backend": "pyautogui"}))
    assert live.health()["status"] == "degraded"


# ── motor stack ───────────────────────────────────────────────────────────────


def test_build_motor_is_null_in_dry_run(tmp_path) -> None:
    stack = build_motor(_settings(tmp_path))
    assert stack.mode == "null" and stack.dry_run is True
    assert "nothing moves" in stack.note
    assert stack.health()["status"] == "ok"


def test_build_motor_honours_an_explicit_null_backend(tmp_path) -> None:
    stack = build_motor(_settings(tmp_path, dry_run=False, computer={"backend": "null"}))
    assert stack.mode == "null" and stack.dry_run is False
    assert "recording only" in stack.note


def test_build_motor_falls_back_when_pyautogui_is_missing(tmp_path) -> None:
    stack = build_motor(_settings(tmp_path, dry_run=False, computer={"backend": "pyautogui"}))
    assert stack.mode == "null", "no pyautogui in this sandbox ⇒ record instead of pretending"
    assert "unavailable" in stack.note or "recording" in stack.note


def test_describe_action_is_json_safe_and_honest() -> None:
    click = describe_action(spec_from_kind(ActionKind.CLICK, {"x": 12, "y": 34}))
    assert click["kind"] == "click" and click["coord"] == {"x": 12, "y": 34} and click["verify"] is True
    typed = describe_action(spec_from_kind(ActionKind.TYPE, {"text": "হ্যালো"}))
    assert typed["kind"] == "type" and typed["text"] == "হ্যালো"
    keys = describe_action(spec_from_kind(ActionKind.HOTKEY, {"keys": ["alt", "tab"]}))
    assert keys["kind"] == "hotkey" and keys["keys"] == ["alt", "tab"]


async def test_motor_stack_records_without_moving_anything(tmp_path) -> None:
    stack = build_motor(_settings(tmp_path))
    result = await stack.execute(spec_from_kind(ActionKind.CLICK, {"x": 10, "y": 10}))
    assert result.success is True and result.retries == 0, "null mode must not burn retries on pixel checks"
    assert len(stack.backend.executed) == 1
    assert stack.stats == {"actions": 1, "succeeded": 1, "failed": 0}
    assert stack.observe().width > 0


# ── tools ─────────────────────────────────────────────────────────────────────


def test_register_computer_tools_adds_typed_specs(tmp_path) -> None:
    settings = _settings(tmp_path)
    bus = StarEventBus()
    registry = StarToolRegistry(settings, bus=bus, import_legacy=False)
    toolkit = register_computer_tools(registry, settings, bus=bus)
    assert len(registry) == 10
    for name in registry.names():
        spec = registry.get(name)
        assert spec.agent.value == "computer" and spec.origin == "star2" and "phase6" in spec.tags
        assert spec.dry_run_safe is True, "dry-run must record the intent, not refuse it"
    assert registry.get("hotkey").risk == "high" and registry.get("mouse_drag").risk == "high"
    assert registry.get("mouse_move").risk == "low" and registry.get("screen_capture").risk == "low"
    assert toolkit.stats["calls"] == 0
    registered = [e for e in bus.history(limit=40) if e.kind == "tool.registered"]
    assert len(registered) == 10, "one event per tool, emitted by the registry"
    assert {event.payload["tool"] for event in registered} == set(registry.names())


def test_tool_handler_refuses_invalid_and_forbidden_actions(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_computer_tools(registry, settings)

    out_of_range = registry.get("mouse_click").handler(99999, 10)
    assert out_of_range["success"] is False and "invalid" in out_of_range["error"]

    off_screen = registry.get("mouse_click").handler(4000, 3000)
    assert off_screen["success"] is False and off_screen["result"]["verdict"]["rule"] == "bounds"

    blocked = registry.get("hotkey").handler(["ctrl", "alt", "del"])
    assert blocked["success"] is False and "blocked_hotkey" in blocked["error"]
    assert toolkit.stats["refused"] == 2 and toolkit.stats["invalid"] == 1


def test_tool_handler_records_the_action_when_dry_run_is_off(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_computer_tools(registry, settings)
    out = registry.get("keyboard_type").handler("hello star")
    assert out["success"] is True
    assert out["result"]["via"] == "agent.motor.null"
    assert out["result"]["action"]["text"] == "hello star"
    assert len(toolkit.motor.backend.executed) == 1


def test_preview_refuses_without_executing(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    toolkit = ComputerToolkit(settings)
    refused = toolkit.preview(ActionKind.CLICK, {"x": 5000, "y": 5})
    assert refused["ok"] is False and refused["verdict"]["rule"] == "bounds"
    assert toolkit.motor.backend.executed == [], "a preview must never move anything"
    allowed = toolkit.preview(ActionKind.TYPE, {"text": "ok"})
    assert allowed["ok"] is True and allowed["action"]["kind"] == "type"


def test_perception_says_so_when_the_desktop_tools_are_missing(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False)
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_computer_tools(registry, settings)
    out = registry.get("screen_capture").handler()
    assert out["success"] is False and "not available here" in out["error"]


async def test_executor_simulates_computer_tools_without_touching_the_motor(tmp_path) -> None:
    settings = _settings(tmp_path)                      # dry-run on
    _agent, executor, _registry, toolkit, audit, _bus, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        result = await executor.call("mouse_click", {"x": 100, "y": 100}, session_id="s1", call_id="c1")
        assert result.decision == "simulated" and result.ok is True
        assert toolkit.motor.backend.executed == [], "dry-run must not reach the motor"
        entry = audit.tail(limit=1)[0]
        assert entry["tool"] == "mouse_click" and entry["arguments"] == {"x": 100, "y": 100}
    finally:
        await executor.aclose()


async def test_executor_audits_a_real_computer_action(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    _agent, executor, _registry, toolkit, audit, _bus, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        result = await executor.call("mouse_move", {"x": 40, "y": 60}, session_id="s1", call_id="c1")
        assert result.decision == "executed" and result.ok is True
        assert len(toolkit.motor.backend.executed) == 1
        entry = audit.tail(limit=1)[0]
        assert entry["decision"] == "executed" and entry["dry_run"] is False and entry["persisted"] is True
    finally:
        await executor.aclose()


async def test_hotkey_needs_an_explicit_confirmation(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    _agent, executor, _registry, _toolkit, _audit, bus, confirmations = _stack(tmp_path, settings)
    await executor.startup()
    try:
        result = await executor.call("hotkey", {"keys": ["alt", "tab"]}, session_id="s1", call_id="c1")
        assert result.decision == "needs_confirmation" and result.confirmation_id
        assert len(confirmations.pending()) == 1
        confirmation = confirmations.get(result.confirmation_id)
        assert confirmation is not None and confirmation.tool == "hotkey"
        approved = await executor.execute_confirmed(confirmation, approved=True)
        assert approved["decision"] == "executed"
        assert confirmations.pending() == []
    finally:
        await executor.aclose()


# ── the agent ─────────────────────────────────────────────────────────────────


def test_agent_selection_covers_three_languages(tmp_path) -> None:
    agent = ComputerAgent(_settings(tmp_path))
    assert agent.name.value == "computer"
    for goal in ("take a screenshot", "click at 10,20", "ছবি তোলো", "স্ক্রিনে কী আছে", "type hello", "scroll down"):
        assert agent.can_handle(goal), goal
    assert agent.score_goal("read https://example.com") == 0.0


@pytest.mark.parametrize(
    ("goal", "actions"),
    [
        ("take a screenshot", ["capture"]),
        ("ছবি তোলো", ["capture"]),
        ("what is on the screen?", ["read_screen"]),
        ("click at 480,320", ["move", "click", "capture"]),
        ("৪৮০,৩২০ এ ক্লিক করো", ["move", "click", "capture"]),
        ("right click at 100,200", ["move", "click", "capture"]),
        ("double click at 100,200", ["move", "click", "capture"]),
        ("type 'hello star' please", ["type", "capture"]),
        ("press enter", ["press", "capture"]),
        ("hotkey alt+tab", ["hotkey", "capture"]),
        ("scroll down 5", ["scroll", "capture"]),
        ("drag to 900,700", ["drag", "capture"]),
        ("wait 3 seconds", ["wait", "capture"]),
        ("move mouse to 20,30", ["move", "capture"]),
    ],
)
def test_agent_plans_physical_goals(tmp_path, goal: str, actions: list[str]) -> None:
    agent = ComputerAgent(_settings(tmp_path))
    assert [step.action for step in agent.plan(goal)] == actions, goal


def test_agent_plans_nothing_for_unrelated_goals(tmp_path) -> None:
    agent = ComputerAgent(_settings(tmp_path))
    assert agent.plan("what is 2+2") == ()
    assert agent.plan("") == ()
    assert agent.plan("read https://example.com") == ()


def test_agent_click_arguments_carry_button_and_clicks(tmp_path) -> None:
    agent = ComputerAgent(_settings(tmp_path))
    steps = agent.plan("right click at 100,200")
    click = next(step for step in steps if step.action == "click")
    assert click.arguments == {"x": 100, "y": 200, "button": "right", "clicks": 1}
    double = next(step for step in agent.plan("double click at 5,5") if step.action == "click")
    assert double.arguments["clicks"] == 2


def test_agent_respects_the_step_budget(tmp_path) -> None:
    agent = ComputerAgent(_settings(tmp_path, computer={"max_steps": 1}))
    assert agent.max_steps == 1
    assert len(agent.plan("click at 480,320")) == 1


async def test_agent_dry_run_records_intent_and_moves_nothing(tmp_path) -> None:
    settings = _settings(tmp_path)
    agent, executor, _registry, toolkit, _audit, bus, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run("click at 480,320", session_id="s1")
        assert run.state == "dry_run" and run.succeeded is True
        assert all(step.decision == "simulated" for step in run.steps)
        assert toolkit.motor.backend.executed == []
        assert "nothing moved" in run.summary
        assert "480" in run.steps[1].verification_note, "the intended coordinate is part of the honest note"
        kinds = [event.kind for event in bus.history(limit=80)]
        assert {"agent.started", "agent.step", "agent.completed"} <= set(kinds)
    finally:
        await executor.aclose()


async def test_agent_is_blocked_by_its_own_guardrails_even_in_dry_run(tmp_path) -> None:
    settings = _settings(tmp_path)                      # dry-run on
    agent, executor, _registry, toolkit, _audit, _bus, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run("click at 4000,3000")
        assert run.state == "blocked" and run.succeeded is False
        assert run.steps[0].observation["blocked_by_policy"] is True
        assert run.steps[0].observation["block_rule"] == "bounds"
        assert toolkit.motor.backend.executed == []
    finally:
        await executor.aclose()


async def test_agent_parks_on_confirmation_and_resumes_after_approval(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    agent, executor, _registry, toolkit, _audit, _bus, confirmations = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run("hotkey alt+tab", session_id="s1")
        assert run.state == "waiting_confirmation" and run.succeeded is False
        assert run.confirmation_id and run.confirmation_id in run.summary
        assert toolkit.motor.backend.executed == []

        approved = await executor.execute_confirmed(confirmations.get(run.confirmation_id), approved=True)
        assert approved["decision"] == "executed"
        assert len(toolkit.motor.backend.executed) == 1
    finally:
        await executor.aclose()


async def test_agent_real_run_records_every_action(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    agent, executor, _registry, toolkit, _audit, _bus, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        run = await agent.run("type 'hello star' and press enter", session_id="s1")
        assert run.state in ("done", "dry_run")
        assert [step.action for step in run.steps if step.ok][:2] == ["type", "press"]
        assert len(toolkit.motor.backend.executed) >= 2
        assert run.result["actions"], "the run reports the actions it took"
        assert run.result["motor"]["mode"] == "null"
    finally:
        await executor.aclose()


async def test_agent_stops_for_the_emergency_stop(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    agent, executor, _registry, _toolkit, _audit, _bus, _conf = _stack(tmp_path, settings)
    agent._stop_gate = lambda: True
    await executor.startup()
    try:
        run = await agent.run("take a screenshot")
        assert run.state == "blocked" and run.steps == [] and "emergency stop" in run.error
    finally:
        await executor.aclose()


async def test_agent_recovery_is_bounded_and_honest(tmp_path) -> None:
    settings = _settings(tmp_path, dry_run=False, computer={"backend": "null"})
    agent, executor, _registry, _toolkit, _audit, _bus, _conf = _stack(tmp_path, settings)
    await executor.startup()
    try:
        from Backend.star.agents.base import AgentStep, AgentStepPlan

        context: dict[str, Any] = {"recoveries": 2}
        step = AgentStep(index=0, action="click", tool="mouse_click", decision="error", error="missed")
        recovered, note = await agent.recover(AgentStepPlan(action="click", tool="mouse_click"), step, context, run=None)  # type: ignore[arg-type]
        assert recovered is False and "budget" in note

        context2: dict[str, Any] = {}
        blocked_step = AgentStep(index=0, action="click", tool="mouse_click", decision="blocked")
        blocked_step.observation = {"blocked_by_policy": True}
        recovered2, note2 = await agent.recover(
            AgentStepPlan(action="click", tool="mouse_click"), blocked_step, context2, run=None  # type: ignore[arg-type]
        )
        assert recovered2 is False and "policy refused" in note2
    finally:
        await executor.aclose()


def test_agent_describe_exposes_motor_and_guardrails(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = StarToolRegistry(settings, import_legacy=False)
    toolkit = register_computer_tools(registry, settings)
    agent = ComputerAgent(settings, registry=registry, toolkit=toolkit)
    described = agent.describe()
    assert described["name"] == "computer" and len(described["tools"]) == 10
    assert described["motor"]["mode"] == "null"
    assert described["guardrails"]["screen"] == "1920x1080"
    assert agent.health()["status"] == "degraded", "no executor ⇒ cannot act"


def test_build_computer_factory(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = StarToolRegistry(settings, import_legacy=False)
    bus = StarEventBus()
    agent = build_computer(settings, bus=bus, registry=registry)
    assert isinstance(agent, ComputerAgent) and agent.toolkit is not None and len(registry) == 10
    assert isinstance(agent.toolkit.motor, MotorStack)
    disabled = build_computer(_settings(tmp_path, computer={"enabled": False}), registry=StarToolRegistry(settings, import_legacy=False))
    assert disabled.toolkit is not None


# ── application wiring ────────────────────────────────────────────────────────


async def test_application_exposes_the_computer_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STAR_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("STAR_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("STAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAR_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("STAR_PATTERNS_PATH", str(tmp_path / "patterns.jsonl"))
    monkeypatch.setenv("STAR_CANDIDATES_PATH", str(tmp_path / "learning" / "candidates.jsonl"))
    monkeypatch.setenv("STAR_FEEDBACK_PATH", str(tmp_path / "learning" / "feedback.jsonl"))
    monkeypatch.setenv("STAR_EPISODES_PATH", str(tmp_path / "episodes.jsonl"))
    monkeypatch.setenv("STAR_COMPUTER_BACKEND", "null")

    from Backend.star.main import build_application

    app = build_application(Settings.from_env(load_env_file=False))
    await app.startup()
    try:
        assert {"browser", "computer"} <= set(app.capabilities())
        assert {agent["name"] for agent in app.agents()} == {"browser", "computer"}
        state = app.computer_state()
        assert state["ok"] is True and state["motor"]["mode"] == "null" and state["dry_run"] is True
        assert state["settings"]["screen"] == "1920x1080"

        run = await app.run_computer_goal("click at 480,320")
        assert run["ok"] is True and run["state"] == "dry_run"
        assert "did not move" in run["note"]
        assert app.computer_state()["last_run"]["run_id"] == run["run_id"]

        blocked = await app.run_computer_goal("click at 9000,9000")
        assert blocked["ok"] is False and blocked["state"] == "blocked"
        assert (await app.run_computer_goal("  "))["error"] == "field 'goal' is required"

        await app.emergency_stop(reason="test")
        stopped = await app.run_computer_goal("take a screenshot")
        assert stopped["ok"] is False and stopped["stopped"] is True
    finally:
        await app.aclose()

"""Phase 4 — tool layer tests: specs, registry, permissions, executor, legacy gate."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from Backend.star.brain.schemas import (
    AgentName,
    Context,
    Plan,
    Task,
    TaskState,
    ToolCall,
    ToolCallState,
    UserRequest,
)
from Backend.star.config.settings import PathsSettings, SecuritySettings, Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.tools.executor import ToolExecutor, build_tool_executor
from Backend.star.tools.legacy_gate import CURRENT_CALL, LegacyToolGate
from Backend.star.tools.permissions import Decision, PermissionEngine
from Backend.star.tools.registry import StarToolRegistry, build_tool_registry
from Backend.star.tools.risk import RISK_ORDER, classify_risk, highest, risk_at_least, risk_rank
from Backend.star.tools.spec import (
    ToolCategory,
    ToolResult,
    ToolSpec,
    agent_for_tool,
    category_for_tool,
    spec_from_function,
    validate_arguments,
)


def _settings(tmp_path, **security: Any) -> Settings:
    return Settings(
        paths=PathsSettings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            workspace_root=tmp_path / "workspace",
            memory_db=tmp_path / "memory.db",
            patterns_path=tmp_path / "patterns.jsonl",
            episodes_path=tmp_path / "episodes.jsonl",
        ),
        security=SecuritySettings(**security) if security else SecuritySettings(),
    )


def _spec(
    name: str = "demo_tool",
    *,
    risk: str = "low",
    handler: Any = None,
    required: list[str] | None = None,
    properties: dict[str, Any] | None = None,
    **extra: Any,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} does things",
        risk=risk,
        handler=handler if handler is not None else (lambda **kwargs: {"success": True, "result": kwargs}),
        parameters={
            "type": "object",
            "properties": properties if properties is not None else {"level": {"type": "number"}},
            "required": required if required is not None else [],
        },
        **extra,
    )


def _kinds(bus: StarEventBus, limit: int = 200) -> list[str]:
    return [event.kind for event in bus.history(limit=limit)]


def _events(bus: StarEventBus, kind: str, limit: int = 200) -> list[Any]:
    return [event for event in bus.history(limit=limit) if event.kind == kind]


# ── risk tiers ────────────────────────────────────────────────────────────────


def test_risk_tiers_are_ordered_low_to_critical() -> None:
    assert RISK_ORDER == ("unknown", "low", "medium", "high", "critical")
    assert risk_rank("critical") > risk_rank("low")
    assert risk_rank("nonsense") == risk_rank("medium"), "unknown tiers rank as medium"
    assert risk_at_least("high", "high") and not risk_at_least("medium", "high")
    assert highest(["low", "critical", "medium"]) == "critical"
    assert highest([]) == "low" and highest([], default="none") == "none"


def test_classify_risk_ignores_free_text_argument_values() -> None:
    assert classify_risk("search_web", {"query": "how to delete everything"}) == "low"
    assert classify_risk("delete_file", {"path": "/tmp/x"}) == "high"
    assert classify_risk("set_volume", {"level": 40}) == "medium"
    assert classify_risk("format_disk") == "critical"
    assert classify_risk("something_new") == "medium"


# ── tool specs ────────────────────────────────────────────────────────────────


def test_tool_spec_rejects_bad_names_and_risks() -> None:
    with pytest.raises(ValueError):
        ToolSpec(name="not a tool!")
    with pytest.raises(ValueError):
        ToolSpec(name="ok_tool", risk="apocalyptic")
    spec = ToolSpec(name="OK_Tool", risk="HIGH")
    assert spec.name == "OK_Tool" and spec.risk == "high"


def test_tool_spec_public_never_leaks_the_handler() -> None:
    spec = _spec("set_volume", risk="medium", category=ToolCategory.MEDIA, agent=AgentName.SYSTEM, tags=("audio",))
    public = spec.public()
    assert "handler" not in public
    assert public["callable"] is True
    assert public["category"] == "media" and public["agent"] == "system"
    assert public["risk"] == "medium" and public["tags"] == ["audio"]
    assert public["required"] == [] and public["dry_run_safe"] is True
    assert json.dumps(public)
    assert spec.schema() == {
        "type": "function",
        "function": {"name": "set_volume", "description": spec.description, "parameters": spec.parameters},
    }
    assert ToolSpec(name="no_handler").callable is False


def test_agent_and_category_mapping() -> None:
    assert agent_for_tool("browser.navigate") is AgentName.BROWSER
    assert agent_for_tool("see_screen") is AgentName.COMPUTER
    assert agent_for_tool("set_volume") is AgentName.SYSTEM
    assert category_for_tool("take_screenshot") is ToolCategory.VISION
    assert category_for_tool("youtube_play_last") is ToolCategory.MEDIA
    assert category_for_tool("remember_fact") is ToolCategory.MEMORY
    assert category_for_tool("wikipedia_search") is ToolCategory.WEB
    assert category_for_tool("mystery") is ToolCategory.OTHER


def test_validate_arguments_coerces_and_reports() -> None:
    spec = _spec(
        "set_volume",
        required=["level"],
        properties={
            "level": {"type": "integer"},
            "mute": {"type": "boolean"},
            "label": {"type": "string"},
            "ratio": {"type": "number"},
        },
    )
    args, errors, notes = validate_arguments(spec, {"level": "40", "mute": "yes", "label": 7, "ratio": "0.5"})
    assert errors == []
    assert args == {"level": 40, "mute": True, "label": "7", "ratio": 0.5}
    assert any("coerced" in note for note in notes)

    _, errors, _ = validate_arguments(spec, {})
    assert errors == ["missing required argument 'level'"]

    _, errors, _ = validate_arguments(spec, {"level": "loud"})
    assert errors and "not a valid integer" in errors[0]

    _, errors, notes = validate_arguments(spec, {"level": 3.5})
    assert errors == ["argument 'level' must be an integer"]

    args, errors, notes = validate_arguments(spec, {"level": 5, "surprise": 1})
    assert errors == [] and args["surprise"] == 1
    assert any("undeclared" in note for note in notes)

    args, errors, _ = validate_arguments(_spec("see", properties={}, required=[]), None)
    assert args == {} and errors == []


def test_spec_from_function_derives_schema_and_metadata() -> None:
    def play_music(query: str, volume: int = 40, shout: bool = False) -> dict[str, Any]:
        """Play a song on YouTube and remember the choice."""
        return {"success": True}

    spec = spec_from_function(play_music)
    assert spec.name == "play_music"
    assert spec.description == "Play a song on YouTube and remember the choice."
    assert spec.required == ["query"]
    assert spec.properties["volume"]["type"] == "integer"
    assert spec.properties["shout"]["type"] == "boolean"
    assert spec.category is ToolCategory.MEDIA
    assert spec.agent is AgentName.SYSTEM
    assert spec.risk == "medium" and spec.origin == "star2"
    assert spec.module.endswith("test_star2_tools")

    custom = spec_from_function(play_music, name="media.play", risk="low", category=ToolCategory.OTHER, timeout_s=3.0)
    assert custom.name == "media.play" and custom.risk == "low" and custom.timeout_s == 3.0
    assert custom.category is ToolCategory.OTHER


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_register_lookup_and_replace() -> None:
    bus = StarEventBus()
    registry = StarToolRegistry(bus=bus, import_legacy=False)
    assert len(registry) == 0 and registry.names() == []
    spec = _spec("set_volume")
    registry.register(spec)
    assert registry.has("set_volume") and "set_volume" in registry
    assert registry.get("set_volume") is spec
    assert registry.get("nope") is None
    with pytest.raises(ValueError):
        registry.register(_spec("set_volume"))
    replacement = _spec("set_volume", risk="high")
    registry.register(replacement, replace=True)
    assert registry.get("set_volume").risk == "high"
    assert registry.unregister("set_volume") is True
    assert registry.unregister("set_volume") is False
    assert _events(bus, "tool.registered")
    assert registry.stats["registered"] == 2 and registry.stats["replaced"] == 1
    assert registry.stats["misses"] == 1


def test_registry_function_decorator() -> None:
    registry = StarToolRegistry(import_legacy=False)

    @registry.register_function(risk="low", category=ToolCategory.META)
    def star_ping(message: str = "hi") -> dict[str, Any]:
        """Answer a ping."""
        return {"success": True, "result": message}

    assert registry.has("star_ping")
    spec = registry.get("star_ping")
    assert spec is not None and spec.risk == "low" and spec.category is ToolCategory.META
    assert spec.handler is star_ping, "the decorator returns the original function"
    assert spec.properties["message"]["type"] == "string"


def test_registry_imports_the_legacy_tools_as_specs() -> None:
    bus = StarEventBus()
    registry = StarToolRegistry(bus=bus)
    legacy = [spec for spec in registry.describe() if spec["origin"] == "legacy"]
    assert len(legacy) >= 20, f"expected the legacy catalogue to be wrapped, got {len(legacy)}"
    volume = registry.get("set_volume")
    assert volume is not None and volume.callable
    assert volume.risk == "medium" and volume.category is ToolCategory.MEDIA
    assert volume.required == ["level"]
    assert registry.get("see_screen").category is ToolCategory.VISION
    assert registry.get("see_screen").agent is AgentName.COMPUTER
    assert registry.import_legacy() == 0, "importing twice must not duplicate"
    assert registry.health()["status"] == "ok"
    assert registry.summary()["legacy_imported"] >= 20


def test_registry_introspection() -> None:
    registry = StarToolRegistry(import_legacy=False)
    registry.register(_spec("browser.navigate", risk="medium", category=ToolCategory.BROWSER, agent=AgentName.BROWSER))
    registry.register(_spec("see_screen", risk="low", category=ToolCategory.VISION, agent=AgentName.COMPUTER))
    registry.register(_spec("format_disk", risk="critical", category=ToolCategory.SYSTEM))

    assert registry.names() == ["browser.navigate", "format_disk", "see_screen"]
    assert [item["name"] for item in registry.list(category=ToolCategory.VISION)] == ["see_screen"]
    assert [item["name"] for item in registry.list(agent="browser")] == ["browser.navigate"]
    assert [item["name"] for item in registry.list(risk="critical")] == ["format_disk"]
    assert registry.by_category()["vision"] == ["see_screen"]
    assert [schema["function"]["name"] for schema in registry.schemas()][0] == "browser.navigate"

    summary = registry.summary()
    assert summary["tools"] == 3
    assert summary["risks"] == {"low": 1, "medium": 1, "critical": 1}
    assert summary["highest_risk"] == "critical"
    assert summary["without_handler"] == []

    assert registry.risk_of("see_screen") == "low"
    assert registry.risk_of("unknown_thing", {"path": "x"}) == "medium", "unregistered tools fall back to the estimate"


# ── permissions ───────────────────────────────────────────────────────────────


def test_permissions_allow_simulate_and_deny(tmp_path) -> None:
    bus = StarEventBus()
    settings = _settings(tmp_path)
    engine = PermissionEngine(settings, bus=bus)

    simulate = engine.check(_spec("set_volume", risk="medium"), {"level": 40}, session_id="s1")
    assert simulate.decision is Decision.SIMULATE and simulate.dry_run is True and simulate.allowed is True
    assert "dry_run" in simulate.rules

    live = engine.check(_spec("set_volume", risk="medium"), {"level": 40}, session_id="s1", dry_run=False)
    assert live.decision is Decision.ALLOW and live.allowed is True

    unknown = engine.check(None, {}, session_id="s1")
    assert unknown.decision is Decision.DENY and "not registered" in unknown.reason

    denied = engine.check(_spec("format_disk", risk="critical"), {}, session_id="s1", dry_run=False)
    assert denied.decision is Decision.DENY and "critical" in denied.reason

    assert _events(bus, "security.decision")
    assert engine.stats["checks"] == 4 and engine.stats["deny"] == 2
    assert engine.describe()["dry_run"] is True
    assert engine.health()["status"] == "ok"


def test_permissions_lists_and_shell_policy(tmp_path) -> None:
    settings = _settings(tmp_path, tool_denylist=("set_volume",), tool_allowlist=())
    engine = PermissionEngine(settings)
    denied = engine.check(_spec("set_volume"), {"level": 1}, dry_run=False)
    assert denied.decision is Decision.DENY and "deny-list" in denied.reason

    allow_only = _settings(tmp_path, tool_allowlist=("see_screen",))
    engine = PermissionEngine(allow_only)
    assert engine.check(_spec("see_screen"), {}, dry_run=False).decision is Decision.ALLOW
    blocked = engine.check(_spec("set_volume"), {"level": 1}, dry_run=False)
    assert blocked.decision is Decision.DENY and "allow-list" in blocked.reason

    shell_off = PermissionEngine(_settings(tmp_path))
    payload = shell_off.check(_spec("run_thing"), {"command": "ls; rm -rf /"}, dry_run=False)
    assert payload.decision is Decision.DENY and "STAR_ALLOW_SHELL=false" in payload.reason

    shell_on = PermissionEngine(_settings(tmp_path, allow_shell=True))
    allowed = shell_on.check(_spec("run_thing"), {"command": "ls -la"}, dry_run=False)
    assert allowed.decision is Decision.ALLOW
    assert shell_on.health()["status"] == "degraded", "shell enabled is a degraded posture"

    nested = shell_off.check(_spec("run_thing"), {"steps": [{"cmd": "a && b"}]}, dry_run=False)
    assert nested.decision is Decision.DENY


def test_permissions_confirmation_gate(tmp_path) -> None:
    bus = StarEventBus()
    store = ConfirmationStore(ttl_s=60)
    engine = PermissionEngine(_settings(tmp_path), bus=bus, confirmations=store)

    first = engine.check(_spec("lock_workstation", risk="high"), {}, session_id="s1", request_id="r1", dry_run=False)
    assert first.decision is Decision.NEEDS_CONFIRMATION and first.requires_confirmation is True
    assert first.confirmation_id and first.confirmation is not None
    assert len(store.pending()) == 1

    again = engine.check(_spec("lock_workstation", risk="high"), {}, session_id="s1", dry_run=False)
    assert again.confirmation_id == first.confirmation_id, "the same call reuses one confirmation"
    assert store.stats["reused"] == 1

    resolved = store.resolve(first.confirmation_id, approve=True)
    assert resolved["ok"] is True
    third = engine.check(
        _spec("lock_workstation", risk="high"), {}, session_id="s1", dry_run=False
    )
    assert third.decision is Decision.ALLOW and any(rule.startswith("confirmed:") for rule in third.rules)

    denied = store.request(tool="shutdown_now", arguments={}, risk="high", session_id="s1")
    store.resolve(denied.confirmation_id, approve=False, note="not now")
    after_deny = engine.check(_spec("shutdown_now", risk="high"), {}, session_id="s1", dry_run=False)
    assert after_deny.decision is Decision.NEEDS_CONFIRMATION, "a denial is not an approval"

    per_tool = engine.check(
        _spec("reboot_machine", risk="medium", confirm_above="medium"), {}, session_id="s1", dry_run=False
    )
    assert per_tool.decision is Decision.NEEDS_CONFIRMATION, "a spec can lower its own threshold"


def test_permissions_rate_limits(tmp_path) -> None:
    fake_now = [1000.0]
    engine = PermissionEngine(
        _settings(tmp_path, max_actions_per_minute=2, max_actions_per_session=3),
        clock=lambda: fake_now[0],
    )
    spec = _spec("set_volume", risk="low")
    assert engine.check(spec, {}, session_id="s1", dry_run=False).decision is Decision.ALLOW
    engine.record_execution("s1")
    assert engine.check(spec, {}, session_id="s1", dry_run=False).decision is Decision.ALLOW
    engine.record_execution("s1")
    limited = engine.check(spec, {}, session_id="s1", dry_run=False)
    assert limited.decision is Decision.DENY and "rate limit" in limited.reason

    fake_now[0] += 61.0                                    # the minute window slides
    assert engine.check(spec, {}, session_id="s1", dry_run=False).decision is Decision.ALLOW
    engine.record_execution("s1")
    exhausted = engine.check(spec, {}, session_id="s1", dry_run=False)
    assert exhausted.decision is Decision.DENY and "session action limit" in exhausted.reason

    engine.reset("s1")
    assert engine.check(spec, {}, session_id="s1", dry_run=False).decision is Decision.ALLOW
    engine.reset()
    assert engine.describe()["sessions_tracked"] == 0


def test_permissions_stop_gate_and_unsimulatable_tools(tmp_path) -> None:
    stopped = {"value": False}
    engine = PermissionEngine(_settings(tmp_path), stop_gate=lambda: stopped["value"])
    spec = _spec("set_volume", risk="low")
    assert engine.check(spec, {}, dry_run=False).decision is Decision.ALLOW
    stopped["value"] = True
    halted = engine.check(spec, {}, dry_run=False)
    assert halted.decision is Decision.DENY and "emergency stop" in halted.reason

    unsafe = _spec("get_device_state", risk="low", dry_run_safe=False)
    engine2 = PermissionEngine(_settings(tmp_path))
    result = engine2.check(unsafe, {}, dry_run=True)
    assert result.decision is Decision.DENY and "cannot be simulated" in result.reason


# ── executor ──────────────────────────────────────────────────────────────────


def _executor(tmp_path, *, dry_run: bool = True, specs: list[ToolSpec] | None = None, **security: Any):
    bus = StarEventBus()
    settings = _settings(tmp_path, dry_run=dry_run, **security)
    registry = StarToolRegistry(settings, bus=bus, import_legacy=False)
    for spec in specs or []:
        registry.register(spec)
    audit = AuditLog(tmp_path / "audit.jsonl", bus=bus)
    confirmations = ConfirmationStore(settings, bus=bus)
    permissions = PermissionEngine(settings, bus=bus, confirmations=confirmations)
    executor = ToolExecutor(
        settings, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=bus
    )
    return executor, bus, audit, confirmations, settings


async def test_executor_simulates_in_dry_run_without_calling_the_handler(tmp_path) -> None:
    def explode(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("dry-run must never call the handler")

    executor, bus, audit, _, _ = _executor(tmp_path, specs=[_spec("set_volume", handler=explode, required=["level"])])
    result = await executor.call("set_volume", {"level": 40}, session_id="s1", request_id="r1", call_id="c1")
    assert result.ok is True and result.decision == "simulated" and result.dry_run is True
    assert result.data["simulated"] is True and result.data["arguments"] == {"level": 40}
    assert result.output.startswith("dry-run: set_volume")
    assert result.audited is True and result.duration_ms >= 0
    assert executor.stats["simulated"] == 1 and executor.stats["executed"] == 0

    entry = audit.tail(limit=1)[0]
    assert entry["tool"] == "set_volume" and entry["decision"] == "simulated"
    assert entry["session_id"] == "s1" and entry["request_id"] == "r1" and entry["call_id"] == "c1"
    assert entry["arguments_fingerprint"]
    event = _events(bus, "tool.call")[0]
    assert event.payload["decision"] == "simulated" and event.payload["dry_run"] is True


async def test_executor_really_runs_when_dry_run_is_off(tmp_path) -> None:
    calls: list[dict[str, Any]] = []

    def set_volume(level: int = 0) -> dict[str, Any]:
        """Set the volume."""
        calls.append({"level": level})
        return {"success": True, "result": {"level": level}, "output": f"volume {level}"}

    executor, bus, audit, _, _ = _executor(
        tmp_path, dry_run=False, specs=[spec_from_function(set_volume, name="set_volume", risk="low")]
    )
    result = await executor.call("set_volume", {"level": "35"}, session_id="s1")
    assert calls == [{"level": 35}], "arguments are coerced before the handler runs"
    assert result.ok is True and result.decision == "executed" and result.dry_run is False
    assert result.data["result"] == {"level": 35} and result.output == "volume 35"
    assert executor.stats["executed"] == 1
    assert audit.tail(limit=1)[0]["decision"] == "executed"
    assert executor.permissions.stats["allow"] == 1


async def test_executor_normalises_odd_handler_returns(tmp_path) -> None:
    executor, _, _, _, _ = _executor(
        tmp_path,
        dry_run=False,
        specs=[
            _spec("returns_none", risk="low", handler=lambda **kw: None),
            _spec("returns_string", risk="low", handler=lambda **kw: "just a string"),
            _spec("returns_object", risk="low", handler=lambda **kw: {"success": False, "error": "device busy"}),
            _spec("returns_exotic", risk="low", handler=lambda **kw: {"success": True, "result": object()}),
            _spec("returns_result", risk="low", handler=lambda **kw: ToolResult(ok=True, tool="returns_result", decision="executed", output="typed")),
        ],
    )
    assert (await executor.call("returns_none", {})).ok is True
    string_result = await executor.call("returns_string", {})
    assert string_result.ok is True and string_result.output == "just a string"
    failure = await executor.call("returns_object", {})
    assert failure.ok is False and failure.error == "device busy" and failure.decision == "executed"
    exotic = await executor.call("returns_exotic", {})
    assert exotic.ok is True and "object object" in json.dumps(exotic.data)
    typed = await executor.call("returns_result", {})
    assert typed.ok is True and typed.output == "typed"


async def test_executor_handles_errors_timeouts_and_unknown_tools(tmp_path) -> None:
    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("device on fire")

    def slow_sync(**kwargs: Any) -> dict[str, Any]:
        time.sleep(0.4)
        return {"success": True}

    executor, bus, audit, _, _ = _executor(
        tmp_path,
        dry_run=False,
        specs=[
            _spec("boom", risk="low", handler=boom),
            _spec("slow", risk="low", handler=slow_sync, timeout_s=0.05),
        ],
    )
    error = await executor.call("boom", {})
    assert error.ok is False and error.decision == "error" and "device on fire" in (error.error or "")

    timeout = await executor.call("slow", {})
    assert timeout.ok is False and timeout.decision == "timeout" and "0.1s" in (timeout.error or "")

    unknown = await executor.call("not_registered", {})
    assert unknown.ok is False and unknown.decision == "unknown_tool"

    invalid = await executor.call("boom", {}, dry_run=True)
    assert invalid.decision == "simulated", "no required args on this spec"

    missing = await executor.call("slow", {}, dry_run=False, timeout_s=0.01)
    assert missing.decision == "timeout"
    assert executor.stats["errors"] == 1 and executor.stats["timeouts"] == 2 and executor.stats["unknown_tool"] == 1
    assert {entry["decision"] for entry in audit.tail(limit=5)} >= {"error", "timeout", "unknown_tool"}


async def test_executor_invalid_arguments_are_refused_before_policy(tmp_path) -> None:
    called: list[str] = []

    def handler(level: int) -> dict[str, Any]:
        called.append(str(level))
        return {"success": True}

    executor, _, audit, _, _ = _executor(
        tmp_path, dry_run=False, specs=[spec_from_function(handler, name="set_volume", risk="low")]
    )
    result = await executor.call("set_volume", {})
    assert result.ok is False and result.decision == "invalid_args"
    assert "missing required argument 'level'" in (result.error or "")
    assert called == [], "the handler never runs with invalid arguments"
    assert audit.tail(limit=1)[0]["decision"] == "invalid_args"


async def test_executor_denies_and_asks_for_confirmation(tmp_path) -> None:
    executor, bus, audit, confirmations, _ = _executor(
        tmp_path, dry_run=False, specs=[_spec("format_disk", risk="critical"), _spec("lock_workstation", risk="high")]
    )
    denied = await executor.call("format_disk", {}, session_id="s1")
    assert denied.ok is False and denied.decision == "denied"

    waiting = await executor.call("lock_workstation", {}, session_id="s1", request_id="r1", task_id="t1")
    assert waiting.ok is False and waiting.decision == "needs_confirmation"
    assert waiting.confirmation_id
    pending = confirmations.pending()
    assert len(pending) == 1 and pending[0]["tool"] == "lock_workstation" and pending[0]["task_id"] == "t1"

    execution = await executor.execute_confirmed(confirmations.get(waiting.confirmation_id), approved=True)
    assert execution["success"] is True and execution["decision"] == "executed" and execution["dry_run"] is False
    assert confirmations.get(waiting.confirmation_id).result["success"] is True

    refused = await executor.execute_confirmed(
        confirmations.request(tool="lock_workstation", arguments={"again": True}, risk="high"), approved=False
    )
    assert refused["ok"] is False and refused["decision"] == "denied"


async def test_executor_runs_a_whole_plan(tmp_path) -> None:
    ran: list[str] = []

    def see_screen(reason: str = "") -> dict[str, Any]:
        ran.append(f"see_screen:{reason}")
        return {"success": True, "result": {"text": "a terminal"}}

    executor, bus, audit, _, settings = _executor(
        tmp_path, dry_run=False, specs=[spec_from_function(see_screen, name="see_screen", risk="low")]
    )
    executor.registry.register(_spec("lock_workstation", risk="high"))
    executor.registry.register(_spec("format_disk", risk="critical"))

    plan = Plan(
        request_id="req_1",
        intent="mixed",
        tasks=[
            Task(
                goal="look",
                agent=AgentName.COMPUTER,
                steps=[ToolCall(tool="see_screen", arguments={"reason": "verify"}, state=ToolCallState.PROPOSED)],
            ),
            Task(
                goal="lock",
                agent=AgentName.SYSTEM,
                risk="high",
                steps=[ToolCall(tool="lock_workstation", state=ToolCallState.PROPOSED)],
            ),
            Task(
                goal="destroy",
                agent=AgentName.SYSTEM,
                risk="critical",
                steps=[ToolCall(tool="format_disk", state=ToolCallState.PROPOSED)],
            ),
            Task(goal="chat", agent=AgentName.CONVERSATION, state=TaskState.DONE),
        ],
    )
    executed = await executor.execute_plan(plan, context=Context(dry_run=False), session_id="s1", request_id="req_1")

    assert ran == ["see_screen:verify"]
    look, lock, destroy, chat = executed.tasks
    assert look.state is TaskState.DONE and look.steps[0].state is ToolCallState.DONE
    assert look.steps[0].attempt == 1 and look.steps[0].result["success"] is True
    assert look.verification.ok is True and "see_screen=done" in look.verification.observed
    assert look.started_at is not None and look.ended_at is not None

    assert lock.state is TaskState.WAITING_CONFIRMATION
    assert lock.steps[0].state is ToolCallState.PROPOSED and lock.steps[0].requires_confirmation is True
    assert lock.steps[0].confirmation_id

    assert destroy.state is TaskState.BLOCKED and destroy.steps[0].state is ToolCallState.DENIED
    assert chat.state is TaskState.DONE and chat.steps == []

    event = _events(bus, "plan.executed")[0]
    assert event.payload["executed"] == 1 and event.payload["denied"] == 1 and event.payload["waiting_confirmation"] == 1
    assert executor.stats["plans"] == 1
    assert len(audit.query(session_id="s1")) >= 3


async def test_executor_skips_calls_that_are_already_finished(tmp_path) -> None:
    calls: list[str] = []
    executor, _, _, _, _ = _executor(
        tmp_path, dry_run=False, specs=[_spec("see_screen", risk="low", handler=lambda **kw: calls.append("run") or {"success": True})]
    )
    plan = Plan(
        tasks=[
            Task(
                goal="look",
                steps=[
                    ToolCall(tool="see_screen", state=ToolCallState.DONE, result={"success": True}),
                    ToolCall(tool="see_screen", state=ToolCallState.PROPOSED),
                ],
            )
        ]
    )
    await executor.execute_plan(plan, session_id="s1")
    assert calls == ["run"], "only the PROPOSED call runs"


async def test_executor_run_call_updates_the_tool_call(tmp_path) -> None:
    executor, _, _, _, _ = _executor(tmp_path, specs=[_spec("set_volume", risk="low", required=["level"])])
    call = ToolCall(tool="set_volume", arguments={"level": 30}, state=ToolCallState.PROPOSED)
    updated = await executor.run_call(call, session_id="s1", request_id="r1", plan_id="p1", task_id="t1")
    assert updated is call
    assert call.state is ToolCallState.DONE and call.attempt == 1
    assert call.result["decision"] == "simulated" and call.result["dry_run"] is True
    assert call.risk == "medium", "argument keys can raise the risk above the spec's own tier"
    assert call.duration_ms >= 0

    denied_call = ToolCall(tool="nope", state=ToolCallState.PROPOSED)
    await executor.run_call(denied_call, session_id="s1", dry_run=False)
    assert denied_call.state is ToolCallState.FAILED and "not registered" in (denied_call.error or "")


async def test_executor_describe_and_health(tmp_path) -> None:
    executor, _, _, _, _ = _executor(tmp_path, specs=[_spec("see_screen", risk="low")])
    await executor.call("see_screen", {}, session_id="s1")
    described = executor.describe()
    assert described["name"] == "tool-executor" and described["tools"] == 1
    assert described["dry_run"] is True and described["calls"] == 1
    assert described["permissions"]["confirm_above_risk"] == "high"
    assert described["audit"]["entries"] == 1
    assert executor.health()["status"] == "ok"

    live = _executor(tmp_path / "live", dry_run=False)[0]
    assert live.health()["status"] == "degraded", "running with dry-run off is a degraded posture"


def test_build_tool_executor_wires_everything(tmp_path) -> None:
    settings = _settings(tmp_path)
    executor = build_tool_executor(settings)
    assert isinstance(executor, ToolExecutor)
    assert isinstance(executor.registry, StarToolRegistry) and len(executor.registry) >= 20
    assert isinstance(executor.permissions, PermissionEngine)
    assert isinstance(executor.audit, AuditLog)
    assert isinstance(executor.confirmations, ConfirmationStore)
    assert executor.permissions.confirmations is executor.confirmations


# ── legacy gate ───────────────────────────────────────────────────────────────


async def test_gate_routes_legacy_execute_tool_through_policy(tmp_path) -> None:
    executor, bus, audit, _, _ = _executor(
        tmp_path, specs=[_spec("set_volume", risk="low", required=["level"], handler=lambda level: {"success": True, "result": {"level": level}})]
    )
    gate = LegacyToolGate(executor)
    assert gate.installed is False, "the shim is opt-in per application startup"

    payload = gate("set_volume", level=40)          # legacy call shape: name + kwargs
    assert payload["success"] is True
    assert payload["decision"] == "simulated" and payload["dry_run"] is True
    assert payload["result"]["simulated"] is True
    assert audit.tail(limit=1)[0]["tool"] == "set_volume"
    assert audit.tail(limit=1)[0]["decision"] == "simulated"
    assert _events(bus, "security.decision"), "the permission engine saw the legacy call"

    unknown = gate("not_a_tool")
    assert unknown["success"] is False and unknown["decision"] == "unknown_tool"
    assert gate.describe()["calls"] == 2


def test_gate_installs_and_uninstalls_across_modules(tmp_path) -> None:
    executor = _executor(tmp_path)[0]
    gate = LegacyToolGate(executor)
    patched = gate.install()
    try:
        assert gate.installed is True
        assert "Backend.tools.registry" in patched
        assert gate.install() == patched, "installing twice is a no-op"

        import Backend.nlu.command_router as router
        import Backend.tools.registry as registry

        assert registry.execute_tool is gate
        if getattr(router, "execute_tool", None) is not None:
            assert router.execute_tool is gate
    finally:
        restored = gate.uninstall()
    assert restored >= 1 and gate.installed is False

    import Backend.tools.registry as registry

    assert callable(registry.execute_tool) and registry.execute_tool is not gate


def test_gate_can_be_disabled(tmp_path) -> None:
    executor = _executor(tmp_path)[0]
    gate = LegacyToolGate(executor, enabled=False)
    assert gate.install() == [] and gate.installed is False
    payload = gate("set_volume", level=10)
    assert payload.get("decision") is None or payload.get("success") in (True, False)
    assert gate.stats["bypassed"] == 1
    assert gate.uninstall() == 0


async def test_gate_propagates_the_current_turn_ids(tmp_path) -> None:
    executor, _, audit, _, _ = _executor(tmp_path, dry_run=False, specs=[_spec("see_screen", risk="low")])
    gate = LegacyToolGate(executor)
    token = CURRENT_CALL.set({"session_id": "sess-9", "request_id": "req-9", "plan_id": "plan-9", "task_id": "task-9"})
    try:
        payload = gate("see_screen")
    finally:
        CURRENT_CALL.reset(token)
    assert payload["success"] is True and payload["decision"] == "executed"
    entry = audit.tail(limit=1)[0]
    assert entry["session_id"] == "sess-9" and entry["request_id"] == "req-9"
    assert entry["plan_id"] == "plan-9" and entry["task_id"] == "task-9"


async def test_gate_survives_an_executor_failure(tmp_path) -> None:
    class BrokenExecutor:
        async def call(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("executor down")

    gate = LegacyToolGate(BrokenExecutor())
    payload = gate("set_volume", level=1)
    assert payload["success"] is False and payload["decision"] == "gate_error"
    assert "executor down" in payload["error"]


async def test_gate_works_when_called_from_the_event_loop_thread(tmp_path) -> None:
    executor, _, _, _, _ = _executor(tmp_path, dry_run=False, specs=[_spec("see_screen", risk="low")])
    gate = LegacyToolGate(executor)
    payload = await asyncio.to_thread(gate, "see_screen")
    assert payload["success"] is True
    direct = gate("see_screen")                       # called on the loop thread → one thread hop
    assert direct["success"] is True
    assert gate.stats["loop_hops"] >= 1


async def test_application_installs_the_gate_and_reports_dry_run(tmp_path) -> None:
    from Backend.star.main import build_application

    settings = _settings(tmp_path)
    app = build_application(settings)
    await app.startup()
    try:
        gate = app.executor.gate
        assert gate.installed is True and gate.enabled is True
        assert "Backend.tools.registry" in gate.describe()["patched_modules"]
        assert "tool.gate_installed" in _kinds(app.bus, 400)

        result = await app.chat("volume 40 koro")
        assert result["ok"] is True
        assert result["simulated"] is True, "dry-run is the default, so nothing really changed"
        assert result["honest_note"].startswith("(dry-run")
        assert result["response"].endswith(result["honest_note"])
        decisions = [call["result"].get("decision") for task in result["tasks"] for call in task["steps"]]
        assert "simulated" in decisions
        assert any(entry["decision"] == "simulated" for entry in app.audit_tail(limit=20))
        assert app.health()["checks"]["executor"]["status"] == "ok"
        assert "executor" in app.capabilities()
    finally:
        await app.aclose()
    assert app.executor.gate.installed is False, "closing the app removes the shim"
    assert "tool.gate_removed" in _kinds(app.bus, 400)


async def test_application_can_run_with_the_gate_disabled(tmp_path) -> None:
    from Backend.star.main import build_application

    settings = _settings(tmp_path, legacy_gate=False)
    app = build_application(settings)
    await app.startup()
    try:
        assert app.executor.gate.enabled is False and app.executor.gate.installed is False
        result = await app.chat("tomar nam ki?")     # no OS action: the gate is off
        assert result["ok"] is True
        assert app.executor.gate.stats["calls"] == 0, "a disabled gate is never consulted"
    finally:
        await app.aclose()

"""Phase 3 — brain tests: schemas, context, reasoning, planning, prediction, reflection."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from Backend.star.brain.context import ContextBuilder, NullRetriever, StoreRetriever, time_of_day
from Backend.star.brain.pipeline import HONEST_NOTES, StarBrain, build_brain, open_memory_store
from Backend.star.brain.planning import RISK_ORDER, StarPlanner, agent_for_tool, default_risk, risk_at_least
from Backend.star.brain.prediction import SAFE_SEED_PATTERNS, HistoryPredictor, NullPredictor, PatternStore
from Backend.star.brain.reflection import NullReflector, StarReflector, call_succeeded
from Backend.star.brain.reasoning import (
    FallbackReasoner,
    ProviderReasoner,
    ReasoningCascade,
    RouterReasoner,
    build_reasoner,
    registered_tools,
    sanitize_actions,
    sanitize_proposed_calls,
)
from Backend.star.brain.schemas import (
    AgentName,
    Context,
    MemoryHit,
    Plan,
    Prediction,
    RawAction,
    Reasoning,
    Reflection,
    Task,
    TaskState,
    ToolCall,
    ToolCallState,
    UserRequest,
    Verification,
)
from Backend.star.config.settings import BrainSettings, PathsSettings, SecuritySettings, Settings
from Backend.star.observability.events import EventPhase, StarEventBus


def _settings(**kwargs: Any) -> Settings:
    """Settings with every filesystem path pointed at a temp dir."""
    tmp = kwargs.pop("tmp", None)
    overrides: dict[str, Any] = dict(kwargs)
    if tmp is not None:
        overrides["paths"] = PathsSettings(
            memory_db=tmp / "memory.db",
            patterns_path=tmp / "patterns.jsonl",
            episodes_path=tmp / "episodes.jsonl",
            workspace_root=tmp / "workspace",
            data_dir=tmp / "data",
            logs_dir=tmp / "logs",
        )
    return Settings(**overrides)


def _kinds(bus: StarEventBus, limit: int = 200) -> list[str]:
    return [event.kind for event in bus.history(limit=limit)]


def _events(bus: StarEventBus, kind: str, limit: int = 200) -> list[Any]:
    return [event for event in bus.history(limit=limit) if event.kind == kind]


# ── schemas ───────────────────────────────────────────────────────────────────


def test_task_and_call_states_match_the_blueprint() -> None:
    assert {state.value for state in TaskState} == {
        "pending",
        "running",
        "waiting_confirmation",
        "verifying",
        "done",
        "failed",
        "cancelled",
        "recovered",
        "blocked",
    }
    assert {state.value for state in ToolCallState} == {
        "proposed",
        "approved",
        "running",
        "done",
        "failed",
        "denied",
        "skipped",
    }
    assert {agent.value for agent in AgentName} == {
        "conversation",
        "research",
        "browser",
        "computer",
        "filesystem",
        "system",
        "coding",
    }
    assert Task(goal="x").is_terminal is False
    assert Task(goal="x", state=TaskState.DONE).is_terminal is True
    assert Task(goal="x", state=TaskState.RECOVERED).is_terminal is True


def test_ids_are_prefixed_and_unique() -> None:
    request = UserRequest(text="hi")
    plan = Plan(request_id=request.request_id, tasks=[Task(goal="g", steps=[ToolCall(tool="t")])])
    assert request.request_id.startswith("req_")
    assert plan.plan_id.startswith("plan_")
    assert plan.tasks[0].task_id.startswith("task_")
    assert plan.tasks[0].steps[0].call_id.startswith("call_")
    assert UserRequest(text="hi").request_id != request.request_id


def test_models_round_trip_through_json() -> None:
    plan = Plan(
        intent="volume",
        language="bn",
        tasks=[
            Task(
                goal="system: set_volume",
                agent=AgentName.SYSTEM,
                state=TaskState.DONE,
                risk="medium",
                steps=[ToolCall(tool="set_volume", arguments={"level": 40}, state=ToolCallState.DONE, result={"success": True})],
                verification=Verification(expected="volume changes", observed="set_volume=done", ok=True),
            )
        ],
        predicted_next=[Prediction(tool="get_volume", confidence=0.35, reason="seed")],
    )
    payload = plan.public()
    assert payload["task_count"] == 1 and payload["all_terminal"] is True
    assert payload["tasks"][0]["agent"] == "system"
    assert payload["tasks"][0]["state"] == "done"
    assert payload["predicted_next"][0]["executed"] is False, "predictions are never executed"
    assert json.loads(json.dumps(payload))["intent"] == "volume"
    assert Plan.model_validate_json(json.dumps(plan.model_dump(mode="json"))).plan_id == plan.plan_id


def test_context_compact_is_prompt_and_ui_safe() -> None:
    context = Context(
        retrieved_memory=[MemoryHit(text="user likes lofi", layer="preference", score=0.8)],
        working_memory=["a", "b", "c", "d", "e", "f", "g"],
        screen_summary="chrome window",
    )
    compact = context.compact()
    assert len(compact["working_memory"]) == 5
    assert compact["retrieved"][0]["layer"] == "preference"
    assert compact["dry_run"] is True
    assert compact["screen_summary"] == "chrome window"


def test_reasoning_has_work() -> None:
    assert Reasoning(intent="x").has_work is False
    assert Reasoning(intent="x", actions=[RawAction(tool="set_volume")]).has_work is True
    assert Reasoning(intent="x", proposed_calls=[{"tool": "see_screen"}]).has_work is True


# ── risk + agent mapping ──────────────────────────────────────────────────────


def test_risk_order_and_comparison() -> None:
    assert RISK_ORDER == ("unknown", "low", "medium", "high", "critical")
    assert risk_at_least("critical", "high") is True
    assert risk_at_least("medium", "high") is False
    assert risk_at_least("high", "high") is True
    assert risk_at_least("low", "unknown") is True
    assert risk_at_least("nonsense", "high") is False


@pytest.mark.parametrize(
    ("tool", "risk"),
    [
        ("get_volume", "low"),
        ("search_web", "low"),
        ("see_screen", "low"),
        ("take_screenshot", "low"),
        ("set_volume", "medium"),
        ("launch_application", "medium"),
        ("play_music", "medium"),
        ("lock_workstation", "high"),
        ("execute_computer_skill", "high"),
        ("execute_autonomous_goal", "high"),
        ("delete_file", "high"),
        ("format_disk", "critical"),
        ("wipe_everything", "critical"),
        ("totally_unknown_thing", "medium"),
    ],
)
def test_default_risk_classification(tool: str, risk: str) -> None:
    assert default_risk(tool) == risk


@pytest.mark.parametrize(
    ("tool", "agent"),
    [
        ("set_volume", AgentName.SYSTEM),
        ("launch_application", AgentName.SYSTEM),
        ("take_screenshot", AgentName.COMPUTER),
        ("see_screen", AgentName.COMPUTER),
        ("search_web", AgentName.RESEARCH),
        ("wikipedia_search", AgentName.RESEARCH),
        ("youtube_open_history_page", AgentName.BROWSER),
        ("recall_memory", AgentName.CONVERSATION),
        ("browser.navigate", AgentName.BROWSER),
        ("file.read", AgentName.FILESYSTEM),
        ("fs.list_dir", AgentName.FILESYSTEM),
        ("computer.click", AgentName.COMPUTER),
        ("code.run_python", AgentName.CODING),
        ("git.push", AgentName.CODING),
        ("system.set_volume", AgentName.SYSTEM),
        ("media.play", AgentName.SYSTEM),
        ("research.papers", AgentName.RESEARCH),
        ("something_with_music", AgentName.SYSTEM),
        ("mystery_tool", AgentName.SYSTEM),
    ],
)
def test_agent_for_tool(tool: str, agent: AgentName) -> None:
    assert agent_for_tool(tool) is agent


# ── output sanitisation (never execute raw model output) ──────────────────────


def test_sanitize_actions_keeps_valid_and_drops_invalid() -> None:
    actions, notes = sanitize_actions(
        [
            {"tool": "set_volume", "args": {"level": 40}, "result": {"success": True}},
            {"tool": "Not A Tool!", "args": {}},
            {"tool": "unregistered_tool", "args": {}},
            "garbage",
        ],
        allowed_tools=["set_volume", "unregistered_tool"],
    )
    assert [action.tool for action in actions] == ["set_volume", "unregistered_tool"]
    assert actions[0].args == {"level": 40}
    assert actions[0].result == {"success": True}
    assert any("invalid tool name" in note for note in notes)
    assert any("malformed" in note for note in notes)


def test_sanitize_actions_blocks_shell_payloads() -> None:
    actions, notes = sanitize_actions(
        [{"tool": "run_thing", "args": {"command": "ls; rm -rf /", "level": 5}}],
        allowed_tools=["run_thing"],
        allow_shell=False,
    )
    assert actions[0].args == {"level": 5}
    assert any("shell-ish" in note for note in notes)

    allowed, _ = sanitize_actions(
        [{"tool": "run_thing", "args": {"command": "ls -la"}}], allowed_tools=["run_thing"], allow_shell=True
    )
    assert allowed[0].args == {"command": "ls -la"}


def test_sanitize_actions_limits_strings_keys_and_depth() -> None:
    actions, notes = sanitize_actions(
        [
            {
                "tool": "set_volume",
                "args": {
                    "long": "x" * 5000,
                    "bad key!": 1,
                    "__proto__": 2,
                    "deep": {"a": {"b": {"c": {"d": "too deep"}}}},
                    "weird": object(),
                },
            }
        ],
        allowed_tools=["set_volume"],
    )
    args = actions[0].args
    assert len(args["long"]) == 4000
    assert "bad key!" not in args
    assert args["deep"] == {"a": {"b": {"c": {"d": None}}}}, "depth is capped, values become None"
    assert isinstance(args["weird"], str)
    assert any("coerced" in note for note in notes)


def test_sanitize_proposed_calls_accepts_llm_shapes() -> None:
    calls, notes = sanitize_proposed_calls(
        [
            {"tool": "set_volume", "arguments": {"level": 30}, "confidence": 0.9},
            {"name": "see_screen", "arguments": '{"reason": "check"}'},
            {"function": {"name": "search_web", "arguments": {"query": "python"}}},
            {"tool": "run_thing", "arguments": {"command": "rm -rf /", "level": 1}},
            {"tool": "unregistered", "arguments": {}},
            {"tool": "bad name!", "arguments": {}},
            "not json",
        ],
        allowed_tools=["set_volume", "see_screen", "search_web", "run_thing"],
        allow_shell=False,
    )
    tools = [call["tool"] for call in calls]
    assert tools == ["set_volume", "see_screen", "search_web", "run_thing"]
    assert calls[0]["arguments"] == {"level": 30} and calls[0]["confidence"] == 0.9
    assert calls[1]["arguments"] == {"reason": "check"}
    assert calls[2]["arguments"] == {"query": "python"}
    assert calls[3]["arguments"] == {"level": 1}, "the shell payload is stripped, not executed"
    assert any("unregistered" in note for note in notes)
    assert any("allow_shell=false" in note for note in notes)


def test_registered_tools_returns_a_list() -> None:
    tools = registered_tools()
    assert isinstance(tools, list)
    assert all(isinstance(name, str) for name in tools)


# ── reasoners ─────────────────────────────────────────────────────────────────


async def test_fallback_reasoner_answers_in_both_languages() -> None:
    reasoner = FallbackReasoner()
    assert reasoner.available() is True
    context = Context(time_of_day="morning")

    bn_greeting = await reasoner.reason(UserRequest(text="হ্যালো star", language="bn"), context)
    assert "শুভ সকাল" in bn_greeting.response_text and bn_greeting.intent == "greeting"
    assert bn_greeting.used_llm is False and bn_greeting.source == "fallback"

    en_thanks = await reasoner.reason(UserRequest(text="thank you!", language="en"), context)
    assert en_thanks.intent == "thanks" and "glad" in en_thanks.response_text

    identity = await reasoner.reason(UserRequest(text="who are you", language="en"), context)
    assert identity.intent == "identity" and "Star" in identity.response_text

    capability = await reasoner.reason(UserRequest(text="what can you do", language="en"), context)
    assert capability.intent == "capability" and "volume" in capability.response_text

    unknown = await reasoner.reason(UserRequest(text="asdf qwer", language="bn"), context)
    assert unknown.intent == "unknown" and unknown.confidence == 0.4


async def test_cascade_uses_the_first_reasoner_that_answers() -> None:
    bus = StarEventBus()
    settings = _settings()

    class Always:
        name = "always"

        def available(self) -> bool:
            return True

        async def reason(self, request: UserRequest, context: Context) -> Reasoning:
            return Reasoning(intent="always", response_text="first wins", source="always")

    class Never:
        name = "never"

        def available(self) -> bool:
            return True

        async def reason(self, request: UserRequest, context: Context) -> Reasoning | None:
            return None

    cascade = ReasoningCascade([Never(), Always()], bus=bus)
    result = await cascade.reason(UserRequest(text="hi"), Context())
    assert result.response_text == "first wins"
    event = _events(bus, "reasoning.complete")[0]
    assert event.payload["reasoner"] == "always"
    assert event.payload["tried"] == ["never:empty"]
    assert cascade.stats["answered"] == 1


async def test_cascade_skips_unavailable_and_swallows_errors() -> None:
    bus = StarEventBus()

    class Unavailable:
        name = "unavailable"

        def available(self) -> bool:
            return False

        async def reason(self, request: UserRequest, context: Context) -> Reasoning:  # pragma: no cover
            raise AssertionError("must be skipped")

    class Exploding:
        name = "exploding"

        def available(self) -> bool:
            return True

        async def reason(self, request: UserRequest, context: Context) -> Reasoning:
            raise RuntimeError("llm on fire")

    class BadAvailable:
        name = "bad_available"

        def available(self) -> bool:
            raise RuntimeError("probe failed")

        async def reason(self, request: UserRequest, context: Context) -> Reasoning:  # pragma: no cover
            raise AssertionError

    cascade = ReasoningCascade([Unavailable(), Exploding(), BadAvailable(), FallbackReasoner()], bus=bus)
    assert cascade.available() is True
    result = await cascade.reason(UserRequest(text="hi", language="bn"), Context())
    assert result.source == "fallback" and result.response_text
    assert cascade.stats["failures"] == 1
    assert [entry["name"] for entry in cascade.describe()] == ["unavailable", "exploding", "bad_available", "fallback"]


async def test_cascade_without_any_answer_still_replies() -> None:
    class Never:
        name = "never"

        def available(self) -> bool:
            return True

        async def reason(self, request: UserRequest, context: Context) -> Reasoning | None:
            return None

    cascade = ReasoningCascade([Never()])
    result = await cascade.reason(UserRequest(text="hi"), Context())
    assert result.source == "none" and result.confidence == 0.0
    assert "all reasoners failed" in result.notes


async def test_router_reasoner_sanitises_real_router_output(monkeypatch) -> None:
    import Backend.nlu.command_router as router_module

    calls: list[str] = []

    def fake_route(text: str) -> dict[str, Any]:
        calls.append(text)
        return {
            "response": "ভলিউম 40 করে দিলাম",
            "source": "command_router",
            "actions": [
                {"tool": "set_volume", "args": {"level": 40}, "result": {"success": True}},
                {"tool": "rm_everything", "args": {}},
                {"tool": "execute_computer_skill", "args": {"command": "ls; rm -rf /", "skill": "see"}},
            ],
        }

    monkeypatch.setattr(router_module, "route_command", fake_route)
    reasoner = RouterReasoner(_settings())
    assert reasoner.available() is True
    result = await reasoner.reason(UserRequest(text="volume 40 koro", language="bn"), Context())
    assert result is not None and calls == ["volume 40 koro"]
    assert result.source == "command_router" and result.used_llm is False
    assert [action.tool for action in result.actions] == ["set_volume", "execute_computer_skill"]
    assert result.actions[1].args == {"skill": "see"}, "shell payload stripped"
    assert result.confidence == 0.95 and result.latency_ms >= 0


async def test_router_reasoner_returns_none_when_nothing_matched(monkeypatch) -> None:
    import Backend.nlu.command_router as router_module

    monkeypatch.setattr(router_module, "route_command", lambda text: {})
    reasoner = RouterReasoner(_settings())
    assert await reasoner.reason(UserRequest(text="hi"), Context()) is None

    def boom(text: str) -> dict[str, Any]:
        raise RuntimeError("router exploded")

    monkeypatch.setattr(router_module, "route_command", boom)
    assert await reasoner.reason(UserRequest(text="hi"), Context()) is None


async def test_provider_reasoner_uses_memory_and_reports_llm_usage() -> None:
    settings = _settings()
    reasoner = ProviderReasoner(settings)

    class FakeProvider:
        def __init__(self) -> None:
            self.seen: list[tuple[str, str]] = []

        def process_query(self, text: str, memory_context: str = "") -> dict[str, Any]:
            self.seen.append((text, memory_context))
            return {
                "response": "হ্যাঁ বন্ধু, করে দিলাম",
                "source": "gemini",
                "actions": [{"tool": "set_volume", "args": {"level": 20}}],
                "tool_calls": [{"tool": "see_screen", "arguments": {"reason": "verify"}}],
            }

    provider = FakeProvider()
    reasoner._provider = provider
    assert reasoner.available() is True and reasoner.provider_name() == "FakeProvider"

    context = Context(memory_block="Relevant: user likes lofi")
    result = await reasoner.reason(UserRequest(text="volume komao", language="bn"), context)
    assert result is not None
    assert provider.seen == [("volume komao", "Relevant: user likes lofi")], "memory is fed to the LLM"
    assert result.used_llm is True and result.source == "gemini"
    assert [action.tool for action in result.actions] == ["set_volume"]
    assert [call["tool"] for call in result.proposed_calls] == ["see_screen"]

    no_memory = await reasoner.reason(UserRequest(text="hi"), Context(memory_block="secret-ish"))
    assert provider.seen[-1][1] == "secret-ish"


async def test_provider_reasoner_survives_timeout_and_errors() -> None:
    reasoner = ProviderReasoner(_settings(), timeout_s=0.05)

    class SlowProvider:
        def process_query(self, text: str, memory_context: str = "") -> dict[str, Any]:
            import time

            time.sleep(0.4)
            return {"response": "too late"}

    reasoner._provider = SlowProvider()
    assert await reasoner.reason(UserRequest(text="hi"), Context()) is None

    class BrokenProvider:
        def process_query(self, text: str, memory_context: str = "") -> dict[str, Any]:
            raise RuntimeError("no route to host")

    reasoner._provider = BrokenProvider()
    assert await reasoner.reason(UserRequest(text="hi"), Context()) is None

    class EmptyProvider:
        def process_query(self, text: str, memory_context: str = "") -> Any:
            return "not a dict"

    reasoner._provider = EmptyProvider()
    assert await reasoner.reason(UserRequest(text="hi"), Context()) is None


def test_build_reasoner_always_ends_with_a_fallback() -> None:
    settings = _settings()
    cascade = build_reasoner(settings)
    names = [reasoner.name for reasoner in cascade.reasoners]
    assert names[-1] == "fallback"
    assert "router" in names
    assert cascade.available() is True

    no_llm = build_reasoner(_settings(brain=BrainSettings(llm_mode="null")))
    assert "provider" not in [reasoner.name for reasoner in no_llm.reasoners]

    custom = build_reasoner(_settings(brain=BrainSettings(reasoning_chain=("fallback",))))
    assert [reasoner.name for reasoner in custom.reasoners] == ["fallback"]


# ── context ───────────────────────────────────────────────────────────────────


def test_time_of_day_buckets() -> None:
    assert time_of_day(datetime(2026, 1, 1, 6)) == "morning"
    assert time_of_day(datetime(2026, 1, 1, 13)) == "afternoon"
    assert time_of_day(datetime(2026, 1, 1, 19)) == "evening"
    assert time_of_day(datetime(2026, 1, 1, 23)) == "night"
    assert time_of_day(datetime(2026, 1, 1, 2)) == "night"


async def test_context_builder_tracks_session_scratch_memory() -> None:
    builder = ContextBuilder(_settings(), session_turns=3)
    request = UserRequest(text="volume barao", session_id="s1")
    await builder.build(request)
    await builder.build(UserRequest(text="volume barao", session_id="s1")),  # duplicate ignored
    await builder.build(UserRequest(text="gaan chalaao", session_id="s1"))
    await builder.build(UserRequest(text="fourth turn", session_id="s1"))
    await builder.build(UserRequest(text="fifth turn", session_id="s1"))
    assert builder.working_memory("s1") == ["gaan chalaao", "fourth turn", "fifth turn"]
    assert builder.working_memory("other") == []
    builder.forget_session("s1")
    assert builder.working_memory("s1") == []
    assert builder.describe()["retriever"] == "null"


async def test_context_builder_retrieves_before_planning_and_emits() -> None:
    bus = StarEventBus()

    class FakeStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def recall_relevant(self, query: str, top_k: int = 5) -> list[Any]:
            self.calls.append(("recall", top_k))

            class Record:
                text = "user likes lofi music"
                kind = type("K", (), {"value": "preference"})()
                score = 0.81
                key = "music_genre"
                value = "lofi"

            return [Record()]

        def build_context_block(self, query: str, *, top_k: int = 5) -> str:
            self.calls.append(("digest", top_k))
            return "Relevant things you know:\n- user likes lofi music"

    store = FakeStore()
    builder = ContextBuilder(_settings(), retriever=StoreRetriever(store), bus=bus, memory_limit=4)
    context = await builder.build(UserRequest(text="gaan chalaao", language="bn", session_id="s1"))
    assert store.calls == [("recall", 4), ("digest", 4)]
    assert len(context.retrieved_memory) == 1
    hit = context.retrieved_memory[0]
    assert hit.layer == "preference" and hit.score == pytest.approx(0.81) and hit.key == "music_genre"
    assert "lofi" in context.memory_block
    assert context.dry_run is True and context.language == "bn"
    event = _events(bus, "context.built")[0]
    assert event.payload["memory_hits"] == 1 and event.payload["retriever"] == "store"
    assert builder.stats["hits"] == 1


async def test_store_retriever_never_raises() -> None:
    class BrokenStore:
        def recall_relevant(self, query: str, top_k: int = 5) -> Any:
            raise RuntimeError("db locked")

        def build_context_block(self, query: str, *, top_k: int = 5) -> str:
            raise RuntimeError("db locked")

    retriever = StoreRetriever(BrokenStore())
    assert await retriever.retrieve("hi") == []
    assert await retriever.digest("hi") == ""
    assert await retriever.retrieve("") == []
    assert NullRetriever().available() is True
    assert await NullRetriever().retrieve("hi") == []
    assert await NullRetriever().digest("hi") == ""


async def test_context_builder_survives_a_failing_retriever() -> None:
    class ExplodingRetriever:
        name = "exploding"

        def available(self) -> bool:
            return True

        async def retrieve(self, query: str, *, limit: int = 6) -> Any:
            raise RuntimeError("boom")

        async def digest(self, query: str, *, limit: int = 6) -> str:
            raise RuntimeError("boom")

    builder = ContextBuilder(_settings(), retriever=ExplodingRetriever())
    context = await builder.build(UserRequest(text="hi"))
    assert context.retrieved_memory == [] and context.memory_block == ""
    assert builder.stats["failures"] == 1


# ── planning ──────────────────────────────────────────────────────────────────


async def test_planner_groups_calls_by_agent_and_adds_a_conversation_task() -> None:
    bus = StarEventBus()
    planner = StarPlanner(_settings(), bus=bus)
    reasoning = Reasoning(
        intent="mixed",
        response_text="সব করে দিলাম",
        source="command_router",
        actions=[
            RawAction(tool="set_volume", args={"level": 40}, result={"success": True}),
            RawAction(tool="take_screenshot", args={}, result={"success": True, "path": "/tmp/x.png"}),
            RawAction(tool="search_web", args={"query": "python"}, result={"success": False, "error": "offline"}),
        ],
    )
    plan = await planner.plan(UserRequest(text="do things", language="bn"), Context(), reasoning)

    assert plan.tasks[0].agent is AgentName.CONVERSATION and plan.tasks[0].steps == []
    assert plan.tasks[0].state is TaskState.DONE and plan.tasks[0].verification.ok is True
    agents = [task.agent for task in plan.tasks[1:]]
    assert agents == [AgentName.SYSTEM, AgentName.COMPUTER, AgentName.RESEARCH]
    assert plan.tasks[1].state is TaskState.DONE
    assert plan.tasks[3].state is TaskState.FAILED
    assert plan.tasks[3].error == "offline"
    assert plan.tasks[3].verification.ok is False
    assert plan.requires_confirmation is False
    assert plan.source == "planner:command_router"
    assert "dry_run=true" in plan.rationale

    event = _events(bus, "plan.created")[0]
    assert event.payload["tasks"] == 4 and event.payload["tool_calls"] == 3
    assert set(event.payload["agents"]) == {"conversation", "system", "computer", "research"}
    assert event.payload["risk"] == "medium"


async def test_planner_marks_risky_tasks_for_confirmation_or_blocks_them() -> None:
    settings = _settings(security=SecuritySettings(confirm_above_risk="high", deny_risk="critical"))
    planner = StarPlanner(settings)
    confirm_plan = await planner.plan(
        UserRequest(text="lock it"),
        Context(dry_run=False),
        Reasoning(response_text="ঠিক আছে", proposed_calls=[{"tool": "lock_workstation", "arguments": {}}]),
    )
    locked = next(task for task in confirm_plan.tasks if task.steps)
    assert locked.state is TaskState.WAITING_CONFIRMATION
    assert locked.risk == "high" and locked.steps[0].requires_confirmation is True
    assert confirm_plan.requires_confirmation is True

    deny_plan = await planner.plan(
        UserRequest(text="format everything"),
        Context(dry_run=False),
        Reasoning(response_text="না!", proposed_calls=[{"tool": "format_disk", "arguments": {}}]),
    )
    denied = next(task for task in deny_plan.tasks if task.steps)
    assert denied.state is TaskState.BLOCKED and denied.risk == "critical"
    assert denied.steps[0].state is ToolCallState.PROPOSED
    assert deny_plan.requires_confirmation is False
    assert planner.stats["confirmations"] == 1 and planner.stats["blocked"] == 1


async def test_planner_uses_an_injected_risk_classifier() -> None:
    planner = StarPlanner(_settings(), risk_classifier=lambda tool, args: "critical" if tool == "set_volume" else "low")
    plan = await planner.plan(
        UserRequest(text="volume"),
        Context(),
        Reasoning(response_text="ok", proposed_calls=[{"tool": "set_volume", "arguments": {"level": 10}}]),
    )
    task = next(task for task in plan.tasks if task.steps)
    assert task.risk == "critical" and task.state is TaskState.BLOCKED

    def broken(tool: str, args: dict[str, Any]) -> str:
        raise RuntimeError("classifier down")

    planner = StarPlanner(_settings(), risk_classifier=broken)
    assert planner.risk_of("set_volume", {}) == "medium"


async def test_planner_truncates_oversized_plans() -> None:
    planner = StarPlanner(_settings(brain=BrainSettings(max_plan_tasks=2, max_tool_calls_per_task=1)))
    reasoning = Reasoning(
        response_text="ok",
        actions=[RawAction(tool=f"set_volume_{i}", args={}, result={"success": True}) for i in range(5)],
    )
    plan = await planner.plan(UserRequest(text="many"), Context(), reasoning)
    assert len(plan.tasks) == 2
    assert all(len(task.steps) <= 1 for task in plan.tasks)
    assert planner.stats["truncated"] >= 1


async def test_planner_handles_an_empty_turn() -> None:
    planner = StarPlanner(_settings())
    plan = await planner.plan(UserRequest(text="hi"), Context(), Reasoning(response_text="হ্যালো!", source="fallback"))
    assert plan.task_count == 1 and plan.tasks[0].agent is AgentName.CONVERSATION
    assert plan.all_terminal is True
    assert plan.intent == "unknown"
    assert "dry_run" in plan.rationale
    assert planner.describe()["risk_classifier"] in {"default_risk", "classify_risk"}


async def test_planner_keeps_proposals_unexecuted() -> None:
    planner = StarPlanner(_settings())
    plan = await planner.plan(
        UserRequest(text="check screen"),
        Context(),
        Reasoning(response_text="দেখছি", proposed_calls=[{"tool": "see_screen", "arguments": {"reason": "verify"}}]),
    )
    call = plan.tasks[1].steps[0]
    assert call.state is ToolCallState.PROPOSED and call.attempt == 0 and call.result == {}
    assert plan.tasks[1].state is TaskState.PENDING
    assert plan.tasks[1].verification.ok is False


# ── prediction ────────────────────────────────────────────────────────────────


def test_pattern_store_learns_and_persists(tmp_path) -> None:
    path = tmp_path / "patterns.jsonl"
    store = PatternStore(path)
    assert store.record(["play_music", "set_volume", "get_volume"]) == 2
    assert store.successors("play_music") == {"set_volume": 1}
    assert store.successors("set_volume") == {"get_volume": 1}
    assert store.total() == 2
    store.record(["play_music", "set_volume"])
    assert store.successors("play_music")["set_volume"] == 2

    reloaded = PatternStore(path)
    assert reloaded.successors("play_music") == {"set_volume": 2}
    assert reloaded.total() == 2, "two distinct pairs, one of them seen twice"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all({"a", "b", "count", "ts"} <= set(entry) for entry in lines)
    assert reloaded.record([]) == 0
    assert reloaded.record(["same", "same"]) == 0
    assert reloaded.snapshot()["play_music"]["set_volume"] == 2


def test_pattern_store_tolerates_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "patterns.jsonl"
    path.write_text('{"a": "x", "b": "y", "count": 2}\nnot json\n{"a": "x"}\n', encoding="utf-8")
    store = PatternStore(path)
    assert store.successors("x") == {"y": 2}


async def test_history_predictor_uses_seed_patterns(tmp_path) -> None:
    bus = StarEventBus()
    settings = _settings(tmp=tmp_path)
    predictor = HistoryPredictor(settings, bus=bus)
    plan = Plan(
        tasks=[Task(goal="system", agent=AgentName.SYSTEM, steps=[ToolCall(tool="play_music", state=ToolCallState.DONE)])]
    )
    predictions = await predictor.predict(UserRequest(text="play music"), Context(), plan)
    assert [prediction.tool for prediction in predictions] == ["set_volume"]
    assert predictions[0].confidence == pytest.approx(0.35)
    assert predictions[0].executed is False and predictions[0].source == "seed"
    assert "often adjust the volume" in predictions[0].reason
    event = _events(bus, "prediction.proposed")[0]
    assert event.payload["note"].startswith("suggestions only")


async def test_history_predictor_learns_from_history(tmp_path) -> None:
    settings = _settings(tmp=tmp_path)
    predictor = HistoryPredictor(settings, min_confidence=0.2)
    store = predictor.store
    store.record(["take_screenshot", "open_notepad"] * 3)
    plan = Plan(tasks=[Task(goal="computer", steps=[ToolCall(tool="take_screenshot", state=ToolCallState.DONE)])])
    predictions = await predictor.predict(UserRequest(text="screenshot"), Context(), plan)
    tools = {prediction.tool: prediction for prediction in predictions}
    assert "open_notepad" in tools, "learned successor must be proposed"
    assert tools["open_notepad"].source == "history"
    assert tools["open_notepad"].confidence == pytest.approx(0.6, abs=0.01)
    assert tools["open_notepad"].confidence > tools["see_screen"].confidence, "history beats a seed"


async def test_history_predictor_refuses_unsafe_tools(tmp_path) -> None:
    settings = _settings(tmp=tmp_path, security=SecuritySettings(confirm_above_risk="high", deny_risk="critical"))
    predictor = HistoryPredictor(settings)
    predictor.store.record(["take_screenshot", "lock_workstation"] * 4)
    predictor.store.record(["take_screenshot", "format_disk"] * 4)
    predictor.store.record(["take_screenshot", "see_screen"] * 4)
    plan = Plan(tasks=[Task(goal="computer", steps=[ToolCall(tool="take_screenshot", state=ToolCallState.DONE)])])
    predictions = await predictor.predict(UserRequest(text="screenshot"), Context(), plan)
    tools = {prediction.tool for prediction in predictions}
    assert "lock_workstation" not in tools and "format_disk" not in tools
    assert "see_screen" in tools
    assert predictor.stats["unsafe_filtered"] >= 2


async def test_history_predictor_can_be_disabled(tmp_path) -> None:
    settings = _settings(tmp=tmp_path, brain=BrainSettings(prediction_enabled=False))
    predictor = HistoryPredictor(settings)
    plan = Plan(tasks=[Task(goal="system", steps=[ToolCall(tool="play_music", state=ToolCallState.DONE)])])
    assert await predictor.predict(UserRequest(text="x"), Context(), plan) == []
    assert predictor.learn(plan) == 0
    assert predictor.describe()["enabled"] is False
    assert await NullPredictor().predict(UserRequest(text="x"), Context(), plan) == []


async def test_history_predictor_needs_a_previous_tool(tmp_path) -> None:
    predictor = HistoryPredictor(_settings(tmp=tmp_path))
    empty_plan = Plan(tasks=[Task(goal="chat", agent=AgentName.CONVERSATION)])
    assert await predictor.predict(UserRequest(text="hi"), Context(), empty_plan) == []


def test_predictor_learns_only_finished_calls(tmp_path) -> None:
    predictor = HistoryPredictor(_settings(tmp=tmp_path))
    plan = Plan(
        tasks=[
            Task(
                goal="system",
                steps=[
                    ToolCall(tool="play_music", state=ToolCallState.DONE),
                    ToolCall(tool="set_volume", state=ToolCallState.FAILED),
                ],
            )
        ]
    )
    assert predictor.learn(plan) == 0, "a failed call is not a pattern worth copying"
    ok_plan = Plan(
        tasks=[
            Task(
                goal="system",
                steps=[
                    ToolCall(tool="play_music", state=ToolCallState.DONE),
                    ToolCall(tool="set_volume", state=ToolCallState.DONE),
                ],
            )
        ]
    )
    assert predictor.learn(ok_plan) == 1
    assert all(len(seed) == 3 for seed in SAFE_SEED_PATTERNS)


# ── reflection ────────────────────────────────────────────────────────────────


def _plan_with(*tasks: Task) -> Plan:
    return Plan(intent="test", tasks=list(tasks))


def _task(
    tool: str,
    *,
    state: TaskState,
    agent: AgentName = AgentName.SYSTEM,
    ok: bool = True,
    risk: str = "low",
    retries: int = 0,
    max_retries: int = 1,
) -> Task:
    call_state = {
        TaskState.DONE: ToolCallState.DONE,
        TaskState.FAILED: ToolCallState.FAILED,
        TaskState.PENDING: ToolCallState.PROPOSED,
        TaskState.BLOCKED: ToolCallState.DENIED,
        TaskState.WAITING_CONFIRMATION: ToolCallState.PROPOSED,
    }[state]
    return Task(
        goal=tool,
        agent=agent,
        state=state,
        retries=retries,
        max_retries=max_retries,
        steps=[ToolCall(tool=tool, state=call_state, result={"success": ok}, risk=risk)],
    )


def test_call_succeeded_rules() -> None:
    assert call_succeeded(ToolCall(tool="t", state=ToolCallState.DONE, result={"success": True})) is True
    assert call_succeeded(ToolCall(tool="t", state=ToolCallState.DONE, result={"success": False})) is False
    assert call_succeeded(ToolCall(tool="t", state=ToolCallState.DONE, result={"ok": True})) is True
    assert call_succeeded(ToolCall(tool="t", state=ToolCallState.DONE, error="boom")) is False
    assert call_succeeded(ToolCall(tool="t", state=ToolCallState.SKIPPED)) is True
    assert call_succeeded(ToolCall(tool="t", state=ToolCallState.PROPOSED)) is False


async def test_reflector_verdicts() -> None:
    bus = StarEventBus()
    reflector = StarReflector(_settings(), bus=bus)
    request = UserRequest(text="do it", language="bn")
    reasoning = Reasoning(intent="x", response_text="করে দিলাম", source="command_router")

    success = await reflector.reflect(request, Context(dry_run=False), _plan_with(_task("set_volume", state=TaskState.DONE)), reasoning)
    assert success.ok is True and success.verdict == "success"
    assert "ঠিকঠাক শেষ" in success.suggestion

    failed = await reflector.reflect(request, Context(dry_run=False), _plan_with(_task("set_volume", state=TaskState.FAILED, ok=False)), reasoning)
    assert failed.ok is False and failed.verdict == "failed" and failed.should_retry is True

    partial = await reflector.reflect(
        request,
        Context(dry_run=False),
        _plan_with(_task("set_volume", state=TaskState.DONE), _task("take_screenshot", state=TaskState.FAILED, ok=False),
                   _task("see_screen", state=TaskState.DONE)),
        reasoning,
    )
    assert partial.verdict == "partial" and partial.ok is False

    blocked = await reflector.reflect(request, Context(), _plan_with(_task("format_disk", state=TaskState.BLOCKED)), reasoning)
    assert blocked.verdict == "blocked" and blocked.should_escalate is True and blocked.ok is False

    waiting = await reflector.reflect(
        request, Context(), _plan_with(_task("lock_workstation", state=TaskState.WAITING_CONFIRMATION)), reasoning
    )
    assert waiting.verdict == "awaiting_confirmation" and waiting.should_escalate is True

    chat_only = await reflector.reflect(
        request, Context(), _plan_with(Task(goal="respond", agent=AgentName.CONVERSATION, state=TaskState.DONE)), reasoning
    )
    assert chat_only.verdict == "no_action" and chat_only.ok is True

    assert len(_events(bus, "reflection.result")) == 6
    assert _events(bus, "reflection.result")[0].payload["verdict"] == "success"


async def test_reflector_recovery_and_escalation_rules() -> None:
    reflector = StarReflector(_settings())
    reasoning = Reasoning(response_text="x")

    computer_failed = await reflector.reflect(
        UserRequest(text="click"),
        Context(dry_run=False),
        _plan_with(_task("computer.click", state=TaskState.FAILED, agent=AgentName.COMPUTER, ok=False)),
        reasoning,
    )
    assert computer_failed.should_recover is True and computer_failed.should_retry is True

    recovered = await reflector.reflect(
        UserRequest(text="click"),
        Context(),
        _plan_with(Task(goal="x", agent=AgentName.BROWSER, state=TaskState.RECOVERED, steps=[ToolCall(tool="browser.open", state=ToolCallState.SKIPPED)])),
        reasoning,
    )
    assert recovered.should_recover is True

    exhausted = await reflector.reflect(
        UserRequest(text="x"),
        Context(dry_run=False),
        _plan_with(_task("set_volume", state=TaskState.FAILED, ok=False, retries=1, max_retries=1)),
        reasoning,
    )
    assert exhausted.should_retry is False and exhausted.should_escalate is True

    dry_run = await reflector.reflect(
        UserRequest(text="x"),
        Context(dry_run=True),
        _plan_with(_task("set_volume", state=TaskState.FAILED, ok=False)),
        reasoning,
    )
    assert dry_run.should_retry is False, "nothing really ran, so there is nothing to retry"


async def test_reflector_verifier_hook_can_downgrade_success() -> None:
    async def verifier(task: Task, context: Context) -> tuple[bool, str]:
        return False, "screen unchanged after set_volume"

    reflector = StarReflector(_settings(), verifier=verifier)
    reflection = await reflector.reflect(
        UserRequest(text="volume", language="en"),
        Context(),
        _plan_with(_task("set_volume", state=TaskState.DONE)),
        Reasoning(response_text="done"),
    )
    assert reflection.verdict == "failed" and "screen unchanged" in reflection.observed
    assert reflection.suggestion.startswith("Could not finish"), "English request → English suggestion"


async def test_reflector_survives_a_broken_verifier_and_can_be_disabled() -> None:
    async def broken(task: Task, context: Context) -> tuple[bool, str]:
        raise RuntimeError("verifier down")

    reflector = StarReflector(_settings(), verifier=broken)
    reflection = await reflector.reflect(
        UserRequest(text="x"), Context(), _plan_with(_task("set_volume", state=TaskState.DONE)), Reasoning(response_text="y")
    )
    assert reflection.verdict == "success"

    disabled = StarReflector(_settings(brain=BrainSettings(reflection_enabled=False)))
    off = await disabled.reflect(UserRequest(text="x"), Context(), _plan_with(), Reasoning(response_text="y"))
    assert off.verdict == "unknown" and off.suggestion == "reflection disabled"
    assert disabled.describe()["enabled"] is False

    null = await NullReflector().reflect(UserRequest(text="x"), Context(), _plan_with(), Reasoning(response_text="y"))
    assert null.ok is True and null.verdict == "unknown"


# ── StarBrain pipeline ────────────────────────────────────────────────────────


class _StubReasoner:
    name = "stub"

    def __init__(self, reasoning: Reasoning | None = None, *, error: Exception | None = None) -> None:
        self.reasoning = reasoning or Reasoning(
            intent="volume",
            response_text="ভলিউম 40 করে দিলাম",
            source="command_router",
            actions=[RawAction(tool="set_volume", args={"level": 40}, result={"success": True})],
        )
        self.error = error
        self.seen: list[tuple[UserRequest, Context]] = []

    def available(self) -> bool:
        return True

    async def reason(self, request: UserRequest, context: Context) -> Reasoning | None:
        self.seen.append((request, context))
        if self.error is not None:
            raise self.error
        return self.reasoning


class _RecordingRetriever:
    name = "recording"

    def __init__(self, hits: list[MemoryHit] | None = None) -> None:
        self.hits = hits or [MemoryHit(text="user likes lofi", layer="preference", score=0.7)]
        self.queries: list[str] = []

    def available(self) -> bool:
        return True

    async def retrieve(self, query: str, *, limit: int = 6) -> list[MemoryHit]:
        self.queries.append(query)
        return self.hits

    async def digest(self, query: str, *, limit: int = 6) -> str:
        return "Relevant: user likes lofi"


class _SilentStore:
    """Stand-in for ``agent.memory.store.MemoryStore`` — keeps SQLite out of unit tests."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed = False

    def remember(self, text: str, *, kind: Any = None) -> Any:
        self.written.append(text)
        return object()

    def recall_relevant(self, query: str, top_k: int = 5) -> list[Any]:
        return []

    def build_context_block(self, query: str, *, top_k: int = 5) -> str:
        return ""

    def close(self) -> None:
        self.closed = True


def _brain(tmp_path, **kwargs: Any) -> tuple[StarBrain, StarEventBus]:
    bus = StarEventBus()
    settings = kwargs.pop("settings", _settings(tmp=tmp_path))
    kwargs.setdefault("retriever", _RecordingRetriever())
    kwargs.setdefault("store", _SilentStore())
    brain = StarBrain(settings, bus=bus, **kwargs)
    return brain, bus


async def test_brain_runs_the_full_pipeline(tmp_path) -> None:
    brain, bus = _brain(tmp_path)
    result = await brain.handle("volume 40 koro", language="bn", session_id="s1")

    assert result["ok"] is True
    assert result["response"], "the real router answered in Bengali"
    assert result["source"] == "brain:command_router"
    assert result["intent"] and result["language"] == "bn"
    assert result["dry_run"] is True
    assert result["memory_hits"] == 1
    assert result["events_published"] is True
    assert result["plan"]["task_count"] == 2
    assert [task["agent"] for task in result["tasks"]] == ["conversation", "system"]
    assert result["task_count"] == 1
    assert result["reflection"]["verdict"] == "success"
    assert result["reasoning"]["used_llm"] is False
    assert result["context"]["retrieved"][0]["layer"] == "preference"
    assert result["latency_ms"] >= 0

    kinds = _kinds(bus, 200)
    assert "context.built" in kinds and "plan.created" in kinds and "reflection.result" in kinds
    assert "brain.turn_started" in kinds and "brain.turn_completed" in kinds
    assert kinds.index("context.built") < kinds.index("plan.created"), "memory is retrieved BEFORE planning"
    assert kinds.index("brain.turn_started") < kinds.index("context.built")
    assert brain.stats["turns"] == 1 and brain.stats["tool_calls"] == 1
    await brain.aclose()


async def test_brain_detects_language_when_not_given(tmp_path) -> None:
    brain, _ = _brain(tmp_path)
    result = await brain.handle("volume 40 koro")
    assert result["language"] == "bn"
    result_en = await brain.handle("what is on my screen")
    assert result_en["language"] == "en"
    await brain.aclose()


async def test_brain_appends_an_honest_note_in_the_reply_language(tmp_path) -> None:
    failing = Reasoning(
        intent="music",
        response_text="গান চালিয়ে দিলাম বন্ধু!",
        source="command_router",
        actions=[RawAction(tool="search_web", args={"query": "lofi"}, result={"success": False, "error": "offline"})],
    )
    brain, bus = _brain(tmp_path, reasoner=_StubReasoner(failing))
    result = await brain.handle("play some music", language="en")
    assert result["ok"] is False
    assert result["reflection"]["verdict"] == "failed"
    assert result["honest_note"] == HONEST_NOTES["bn"]["failed"], "reply is Bengali → note is Bengali"
    assert result["response"].endswith(HONEST_NOTES["bn"]["failed"])

    english = Reasoning(
        intent="music",
        response_text="Playing your song now!",
        actions=[RawAction(tool="search_web", args={}, result={"success": False, "error": "offline"})],
    )
    brain_en, _ = _brain(tmp_path, reasoner=_StubReasoner(english))
    result_en = await brain_en.handle("play music", language="en")
    assert result_en["honest_note"] == HONEST_NOTES["en"]["failed"]
    await brain.aclose()
    await brain_en.aclose()


async def test_brain_never_raises_on_a_reasoner_failure(tmp_path) -> None:
    brain, bus = _brain(tmp_path, reasoner=_StubReasoner(error=RuntimeError("llm exploded")))
    result = await brain.handle("volume 40 koro", language="bn")
    assert result["ok"] is False and "llm exploded" in result["error"]
    assert result["source"] == "brain:error"
    assert result["response"], "Star always answers"
    assert "task.failed" in _kinds(bus)
    assert brain.stats["failures"] == 1
    await brain.aclose()


async def test_brain_writes_an_episode_and_keeps_session_context(tmp_path) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.written: list[tuple[str, Any]] = []
            self.closed = False

        def remember(self, text: str, *, kind: Any = None) -> Any:
            self.written.append((text, kind))
            return object()

        def recall_relevant(self, query: str, top_k: int = 5) -> list[Any]:
            return []

        def build_context_block(self, query: str, *, top_k: int = 5) -> str:
            return ""

        def close(self) -> None:
            self.closed = True

    store = FakeStore()
    brain, bus = _brain(tmp_path, store=store, retriever=StoreRetriever(store))
    await brain.handle("volume 40 koro", session_id="s1", language="bn")
    assert len(store.written) == 1
    text, kind = store.written[0]
    assert text.startswith("volume 40 koro →") and "set_volume" in text
    assert getattr(kind, "value", kind) == "episode"
    assert "memory.updated" in _kinds(bus)
    assert brain.context_builder.working_memory("s1") == ["volume 40 koro"]

    await brain.handle("আরেকবার বলো", session_id="s1", language="bn")
    assert len(brain.context_builder.working_memory("s1")) == 2
    await brain.aclose()
    assert store.closed is True


async def test_brain_survives_a_failing_memory_write(tmp_path) -> None:
    class BrokenStore:
        def remember(self, text: str, *, kind: Any = None) -> Any:
            raise RuntimeError("disk full")

        def recall_relevant(self, query: str, top_k: int = 5) -> list[Any]:
            return []

        def build_context_block(self, query: str, *, top_k: int = 5) -> str:
            return ""

    brain, _ = _brain(tmp_path, store=BrokenStore(), retriever=StoreRetriever(BrokenStore()))
    result = await brain.handle("volume 40 koro", language="bn")
    assert result["ok"] is True, "a memory failure must not break the reply"
    await brain.aclose()


async def test_brain_uses_the_executor_seam_for_proposals(tmp_path) -> None:
    class Executor:
        name = "stub-orchestrator"

        def __init__(self) -> None:
            self.plans: list[Plan] = []

        async def execute(self, plan: Plan, *, context: Context | None = None) -> Plan:
            self.plans.append(plan)
            for task in plan.tasks:
                for call in task.steps:
                    if call.state is ToolCallState.PROPOSED:
                        call.state = ToolCallState.DONE
                        call.result = {"success": True, "executed_by": "stub"}
                task.state = TaskState.DONE
            return plan

    executor = Executor()
    reasoning = Reasoning(response_text="দেখছি", proposed_calls=[{"tool": "see_screen", "arguments": {}}])
    brain, _ = _brain(tmp_path, reasoner=_StubReasoner(reasoning), executor=executor)
    result = await brain.handle("screen e ki ache", language="bn")
    assert len(executor.plans) == 1
    assert result["tasks"][1]["steps"][0]["state"] == "done"
    assert result["reflection"]["verdict"] == "success"

    no_proposals = Reasoning(response_text="ঠিক আছে", actions=[RawAction(tool="set_volume", result={"success": True})])
    brain2, _ = _brain(tmp_path, reasoner=_StubReasoner(no_proposals), executor=executor)
    await brain2.handle("volume 40", language="bn")
    assert len(executor.plans) == 1, "executor is only called when something is still PROPOSED"
    await brain.aclose()
    await brain2.aclose()


async def test_brain_predicts_and_feeds_the_next_turn(tmp_path) -> None:
    brain, bus = _brain(tmp_path)
    reasoning = Reasoning(
        response_text="গান চালু",
        actions=[RawAction(tool="play_music", args={}, result={"success": True})],
    )
    brain.reasoner = _StubReasoner(reasoning)
    result = await brain.handle("gaan chalaao", language="bn")
    assert [prediction["tool"] for prediction in result["predictions"]] == ["set_volume"]
    assert result["plan"]["predicted_next"][0]["executed"] is False
    assert brain.predictor.store.total() == 0, "a single tool call makes no pair"

    brain.reasoner = _StubReasoner(
        Reasoning(response_text="ভলিউম ঠিক", actions=[RawAction(tool="set_volume", args={}, result={"success": True})])
    )
    await brain.handle("volume thik koro", language="bn")
    assert brain.predictor.store.successors("play_music") == {"set_volume": 1}
    assert "prediction.proposed" in _kinds(bus)
    await brain.aclose()


async def test_brain_learns_cross_turn_patterns_only_from_success(tmp_path) -> None:
    brain, _ = _brain(tmp_path)
    brain.reasoner = _StubReasoner(
        Reasoning(response_text="গান চালু", actions=[RawAction(tool="play_music", result={"success": True})])
    )
    await brain.handle("gaan chalaao", language="bn", session_id="s1")
    assert brain.predictor.store.total() == 0

    brain.reasoner = _StubReasoner(
        Reasoning(response_text="ভলিউম ঠিক", actions=[RawAction(tool="set_volume", result={"success": True})])
    )
    await brain.handle("volume thik koro", language="bn", session_id="s1")
    assert brain.predictor.store.successors("play_music") == {"set_volume": 1}, "turn N → turn N+1 is learned"

    # a failed turn must not teach the pattern
    brain.reasoner = _StubReasoner(
        Reasoning(response_text="চেষ্টা করছি", actions=[RawAction(tool="search_web", result={"success": False})])
    )
    await brain.handle("kichu ekta khojo", language="bn", session_id="s1")
    assert brain.predictor.store.successors("set_volume") == {}

    # another session starts from scratch
    brain.reasoner = _StubReasoner(
        Reasoning(response_text="হলুদ", actions=[RawAction(tool="set_brightness", result={"success": True})])
    )
    await brain.handle("brightness barao", language="bn", session_id="s2")
    assert brain.predictor.store.successors("search_web") == {}
    await brain.aclose()


async def test_brain_lifecycle_and_introspection(tmp_path) -> None:
    brain, bus = _brain(tmp_path)
    await brain.startup()
    await brain.startup()  # idempotent
    assert "agent.ready" in _kinds(bus)
    health = brain.health()
    assert health["status"] == "ok"
    assert health["detail"]["reasoners"] == ["router", "provider", "fallback"] or health["detail"]["reasoners"]
    described = brain.describe()
    assert described["phase"] == 3 and described["memory"] == "recording"
    assert described["planner"]["max_tasks"] == 8
    assert described["predictor"]["enabled"] is True
    assert described["reflector"]["enabled"] is True

    await brain.handle("volume 40 koro", session_id="s1", language="bn")
    snapshot = brain.snapshot("s1")
    assert snapshot["ok"] is True and snapshot["working_memory"] == ["volume 40 koro"]
    assert snapshot["recent_plans"][0]["agent"] == "conversation"
    await brain.aclose()


async def test_brain_opens_the_real_memory_store(tmp_path) -> None:
    settings = _settings(tmp=tmp_path)
    store = open_memory_store(settings)
    assert store is not None
    brain = StarBrain(settings, store=store, retriever=StoreRetriever(store))
    await brain.startup()
    result = await brain.handle("amar pochonder gaan lofi music", language="bn", session_id="s1")
    assert result["memory_hits"] == 0, "nothing is stored on the first turn"
    second = await brain.handle("amar pochonder gaan lofi music chalaao", language="bn", session_id="s1")
    assert second["memory_hits"] >= 1, "the previous turn is retrievable before planning"
    assert (tmp_path / "memory.db").exists()
    assert brain.health()["status"] == "ok"
    await brain.aclose()


async def test_build_brain_injects_defaults(tmp_path) -> None:
    settings = _settings(tmp=tmp_path)
    brain = build_brain(settings)
    assert isinstance(brain, StarBrain)
    assert isinstance(brain.planner, StarPlanner)
    assert isinstance(brain.predictor, HistoryPredictor)
    assert isinstance(brain.reflector, StarReflector)
    assert isinstance(brain.reasoner, ReasoningCascade)

    stub = _StubReasoner()
    custom = build_brain(settings, reasoner=stub, retriever=NullRetriever(), store=_SilentStore())
    assert custom.reasoner is stub and isinstance(custom.retriever, NullRetriever)

    no_prediction = build_brain(_settings(tmp=tmp_path, brain=BrainSettings(prediction_enabled=False)), retriever=NullRetriever())
    assert isinstance(no_prediction.predictor, NullPredictor)
    await brain.aclose()


def test_open_memory_store_tolerates_a_bad_path(tmp_path) -> None:
    settings = _settings(tmp=tmp_path)
    broken = settings.model_copy(update={"paths": settings.paths.model_copy(update={"memory_db": tmp_path})})
    assert open_memory_store(broken) is None, "a directory is not a database — degrade, do not crash"


async def test_brain_result_is_json_serialisable(tmp_path) -> None:
    brain, _ = _brain(tmp_path)
    result = await brain.handle("volume 40 koro", language="bn")
    payload = json.dumps(result, ensure_ascii=False, default=str)
    assert "reflection" in payload and "plan" in payload
    assert asyncio.iscoroutinefunction(brain.handle)
    await brain.aclose()

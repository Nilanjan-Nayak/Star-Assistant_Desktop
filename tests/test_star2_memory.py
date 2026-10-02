"""Phase 8 — layered memory: layers, hybrid retrieval, manager, tools, wiring.

Everything here runs against temporary stores (SQLite file, JSONL episode log, JSONL
pattern log) so the repo's own ``star_memory.db`` / ``episodes.jsonl`` are never touched.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from Backend.star.agents.base import AgentRun, AgentStep
from Backend.star.brain.schemas import AgentName, MemoryHit
from Backend.star.config.settings import MemorySettings, PathsSettings, SecuritySettings, Settings
from Backend.star.memory.layers import (
    LAYER_NAMES,
    EpisodicLayer,
    PreferenceLayer,
    ProceduralLayer,
    SemanticLayer,
    WorkingMemory,
    token_overlap,
)
from Backend.star.memory.manager import MemoryManager, build_memory, layer_of_kind
from Backend.star.memory.retrieval import RetrievalTrace, blend, hybrid_retrieve
from Backend.star.memory.tools import MemoryToolkit, register_memory_tools
from Backend.star.observability.events import EVENT_KINDS, StarEventBus
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.permissions import PermissionEngine
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import ConfirmationStore

# ── helpers ───────────────────────────────────────────────────────────────────


def _settings(tmp_path, *, memory: dict[str, Any] | None = None, **security: Any) -> Settings:
    return Settings(
        paths=PathsSettings(
            data_dir=tmp_path / "data",
            logs_dir=tmp_path / "logs",
            workspace_root=tmp_path / "workspace",
            memory_db=tmp_path / "memory.db",
            episodes_path=tmp_path / "episodes.jsonl",
            patterns_path=tmp_path / "patterns.jsonl",
            browser_profile_root=tmp_path / "profiles",
        ),
        memory=MemorySettings(**(memory or {})),
        security=SecuritySettings(audit_path=str(tmp_path / "audit.jsonl"), **security),
        session_id="test-session",
        owner="Tester",
    )


def _store(tmp_path):
    from agent.memory.store import MemoryStore

    return MemoryStore(path=tmp_path / "memory.db", owner="Tester")


def _episode_log(tmp_path, *, max_recall: int = 5):
    from agent.planning.memory import EpisodicMemory

    return EpisodicMemory(path=tmp_path / "episodes.jsonl", max_recall=max_recall)


def _pattern_log(tmp_path):
    from Backend.star.brain.prediction import PatternStore

    return PatternStore(tmp_path / "patterns.jsonl")


def _hit(text: str, layer: str, score: float, *, kind: str = "fact") -> MemoryHit:
    return MemoryHit(text=text, layer=layer, kind=kind, score=score)


class FakeScratch:
    """Stands in for the brain's ``ContextBuilder`` (working-memory provider)."""

    def __init__(self) -> None:
        self.turns: dict[str, list[str]] = {}
        self.forgotten: list[str] = []
        self.fail = False

    def remember_turn(self, session_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("scratch is broken")
        self.turns.setdefault(session_id, []).append(text)

    def working_memory(self, session_id: str) -> list[str]:
        if self.fail:
            raise RuntimeError("scratch is broken")
        return list(self.turns.get(session_id, []))

    def forget_session(self, session_id: str) -> None:
        self.forgotten.append(session_id)
        self.turns.pop(session_id, None)


class BoomLayer:
    name = "semantic"

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        raise RuntimeError("store exploded")


class SlowLayer:
    name = "episodic"

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        import time

        time.sleep(0.4)
        return [_hit("late answer", self.name, 0.9)]


class QuietLayer:
    def __init__(self, name: str, hits: list[MemoryHit]) -> None:
        self.name = name
        self.hits = hits
        self.asked: list[str] = []

    def recall(self, query: str, *, limit: int = 5, session_id: str = "") -> list[MemoryHit]:
        self.asked.append(query)
        return list(self.hits)[:limit]


@pytest.fixture
def store(tmp_path):
    handle = _store(tmp_path)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def bus() -> StarEventBus:
    return StarEventBus(history_size=300)


@pytest.fixture
def memory(tmp_path, bus) -> MemoryManager:
    """A sync-friendly manager over a real (temporary) long-term store."""
    manager = build_memory(_settings(tmp_path), bus=bus, store=_store(tmp_path), own_store=True)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
async def started(tmp_path, bus) -> MemoryManager:
    """A manager that opened its own stores, exactly like the application does."""
    manager = build_memory(_settings(tmp_path), bus=bus)
    await manager.startup()
    try:
        yield manager
    finally:
        await manager.aclose()


def _bare_manager(tmp_path, bus: StarEventBus, monkeypatch: pytest.MonkeyPatch, **memory: Any) -> MemoryManager:
    """A manager with *no* backing store: ``None`` arguments mean "open your own"."""
    monkeypatch.setattr(MemoryManager, "_open_episodes", lambda self: None)
    monkeypatch.setattr(MemoryManager, "_open_patterns", lambda self: None)
    return build_memory(_settings(tmp_path, memory=memory or None), bus=bus, store=None, own_store=False)


def _kinds(bus: StarEventBus, prefix: str = "memory.") -> list[str]:
    return [event.kind for event in bus.history(limit=300) if event.kind.startswith(prefix)]


def _payloads(bus: StarEventBus, kind: str) -> list[dict[str, Any]]:
    return [event.payload for event in bus.history(limit=300) if event.kind == kind]


# ── working memory ────────────────────────────────────────────────────────────


def test_working_memory_keeps_a_bounded_number_of_turns(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path, memory={"working_turns": 3}))
    for index in range(6):
        working.remember_turn("s1", f"turn {index}")
    assert working.turns_for("s1") == ["turn 3", "turn 4", "turn 5"]
    assert working.count("s1") == 3
    assert working.count("other-session") == 0


def test_working_memory_recall_prefers_matching_and_recent_turns(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path))
    working.remember_turn("s1", "please open the browser")
    working.remember_turn("s1", "now play some music")
    hits = working.recall("open the browser", session_id="s1", limit=5)
    assert hits and hits[0].layer == "working" and hits[0].kind == "turn"
    assert "browser" in hits[0].text
    assert all(-1.0 <= hit.score <= 1.0 for hit in hits)


def test_working_memory_forget_clears_one_session_only(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path))
    working.remember_turn("s1", "one")
    working.remember_turn("s2", "two")
    assert working.forget("s1") == 1
    assert working.turns_for("s1") == [] and working.turns_for("s2") == ["two"]


def test_working_memory_uses_the_brain_scratch_when_attached(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path))
    scratch = FakeScratch()
    working.remember_turn("s1", "kept locally")
    assert working.attach(scratch) is True and working.delegated is True
    assert working.turns_for("s1") == []            # the private copy is dropped, not duplicated
    working.remember_turn("s1", "kept in the brain")
    assert scratch.turns["s1"] == ["kept in the brain"]
    assert working.turns_for("s1") == ["kept in the brain"]
    working.forget("s1")
    assert scratch.forgotten == ["s1"]
    assert "brain" in working.describe("s1")["store"]


def test_working_memory_attach_refuses_an_object_that_is_not_a_scratch(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path))
    assert working.attach(object()) is False and working.delegated is False
    assert working.attach(None) is False


def test_working_memory_falls_back_to_its_own_deque_when_the_provider_fails(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path))
    scratch = FakeScratch()
    working.attach(scratch)
    scratch.fail = True
    working.remember_turn("s1", "still remembered")   # must not raise
    scratch.fail = False
    assert working.turns_for("s1") == []              # provider answer wins when it works
    assert working.stats["turns"] >= 1


def test_working_memory_resolves_the_session_that_last_spoke(tmp_path) -> None:
    working = WorkingMemory(_settings(tmp_path))
    working.remember_turn("session-a", "first")
    working.remember_turn("session-b", "second")
    assert working.last_session == "session-b"
    assert [hit.text for hit in working.recall("second", limit=3)] == ["second"]


# ── episodic ──────────────────────────────────────────────────────────────────


def test_episodic_record_writes_the_jsonl_log(tmp_path) -> None:
    layer = EpisodicLayer(_episode_log(tmp_path))
    episode = layer.record(
        "open gmail and send the report",
        steps=[{"tool": "browser.open", "arguments": {"url": "https://example.com"}, "ok": True}],
        succeeded=True,
    )
    assert episode.goal == "open gmail and send the report"
    assert episode.step_count == 1 and episode.succeeded is True
    assert layer.count() == 1
    lines = (tmp_path / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "browser.open" in lines[0]


def test_episodic_recall_returns_only_succeeded_episodes(tmp_path) -> None:
    layer = EpisodicLayer(_episode_log(tmp_path))
    layer.record("send the weekly report by email", steps=[{"tool": "browser.open", "ok": True}], succeeded=True)
    layer.record("send the weekly report by email", steps=[{"tool": "browser.open", "ok": False}], succeeded=False)
    hits = layer.recall("send the weekly report", limit=5)
    assert len(hits) == 1
    assert hits[0].layer == "episodic" and hits[0].kind == "episode"
    assert hits[0].value == "succeeded" and "browser.open" in hits[0].text


def test_episodic_recent_lists_newest_first(tmp_path) -> None:
    layer = EpisodicLayer(_episode_log(tmp_path))
    layer.record("first goal")
    layer.record("second goal", succeeded=False)
    recent = layer.recent(limit=5)
    assert [item["goal"] for item in recent] == ["second goal", "first goal"]
    assert recent[0]["succeeded"] is False and recent[1]["succeeded"] is True


def test_episodic_record_requires_a_goal(tmp_path) -> None:
    layer = EpisodicLayer(_episode_log(tmp_path))
    with pytest.raises(ValueError):
        layer.record("   ")


def test_episodic_sanitises_step_parameters(tmp_path) -> None:
    layer = EpisodicLayer(_episode_log(tmp_path))
    episode = layer.record(
        "convert a file",
        steps=[
            {"tool": "files.convert", "arguments": {"path": Path("/tmp/x.pdf"), "flags": {"a": 1}}, "ok": True},
            {"tool": "", "arguments": "not-a-mapping", "ok": False},
            "a bare string step",
        ],
    )
    assert episode.step_count == 3
    assert episode.steps[0].params["path"] == "/tmp/x.pdf"
    assert episode.steps[1].params == {}
    assert episode.steps[2].skill == "a bare string step"[:64]
    assert json.loads((tmp_path / "episodes.jsonl").read_text(encoding="utf-8").splitlines()[0])["goal"]


def test_episodic_survives_an_unwritable_log(tmp_path) -> None:
    broken = SimpleNamespace(
        save=lambda episode: (_ for _ in ()).throw(OSError("disk full")),
        recall_similar=lambda goal: [],
        size=0,
        _episodes=[],
    )
    layer = EpisodicLayer(broken)
    episode = layer.record("goal that cannot be persisted")   # must not raise
    assert episode.goal.startswith("goal")
    assert layer.recall("goal") == []


def test_episodic_without_a_log_reports_unavailable(tmp_path) -> None:
    layer = EpisodicLayer(None)
    assert layer.available() is False and layer.count() == 0
    assert layer.recall("anything") == [] and layer.recent() == []
    assert layer.describe()["count"] == 0


# ── semantic + preferences ────────────────────────────────────────────────────


def test_semantic_remember_and_recall_only_facts(store) -> None:
    from agent.memory.store import MemoryKind

    layer = SemanticLayer(store)
    layer.remember("Nilanjan drinks black coffee")
    store.remember("reply in Bengali", kind=MemoryKind.PREFERENCE, key="language", value="bn")
    hits = layer.recall("coffee", limit=5)
    assert [hit.text for hit in hits] == ["Nilanjan drinks black coffee"]
    assert hits[0].layer == "semantic" and hits[0].kind == "fact"
    assert layer.count() == 1                       # the preference row is not a fact
    assert layer.recent(limit=5)[0]["kind"] == "fact"


def test_semantic_needs_a_store() -> None:
    layer = SemanticLayer(None)
    assert layer.available() is False
    with pytest.raises(RuntimeError):
        layer.remember("anything")
    assert layer.recall("anything") == [] and layer.count() == 0


def test_semantic_describe_reports_the_scan_window(store) -> None:
    layer = SemanticLayer(store, recent_scan=25)
    layer.remember("a fact")
    described = layer.describe()
    assert described["count"] == 1 and "MemoryStore" in described["store"]
    assert "25" in described["detail"] and "1 record(s)" in described["detail"]


def test_preference_latest_value_wins(store) -> None:
    layer = PreferenceLayer(store)
    layer.set("reply_language", "en")
    layer.set("reply_language", "bn")
    assert layer.get("reply_language") == "bn"
    listed = layer.all(limit=10)
    assert len(listed) == 1 and listed[0]["value"] == "bn"
    assert layer.count() == 1


def test_preference_recall_matches_the_key(store) -> None:
    layer = PreferenceLayer(store)
    layer.set("reply_language", "bn")
    layer.set("coffee", "black, no sugar")
    hits = layer.recall("what is my reply language", limit=5)
    assert hits and hits[0].key == "reply_language"
    assert hits[0].layer == "preference" and hits[0].kind == "preference"


def test_preference_rejects_an_empty_key_or_value(store) -> None:
    layer = PreferenceLayer(store)
    with pytest.raises(ValueError):
        layer.set("", "value")
    with pytest.raises(ValueError):
        layer.set("key", "   ")
    with pytest.raises(RuntimeError):
        PreferenceLayer(None).set("key", "value")


def test_preference_describe_counts_what_it_stored(store) -> None:
    layer = PreferenceLayer(store)
    layer.set("theme", "dark")
    described = layer.describe()
    assert described["count"] == 1 and "1 set here" in described["detail"]


# ── procedural patterns ───────────────────────────────────────────────────────


def test_procedural_records_pairs_and_answers_about_a_tool(tmp_path) -> None:
    layer = ProceduralLayer(_pattern_log(tmp_path))
    layer.record(["set_volume", "get_system_status"])
    layer.record(["set_volume", "get_system_status"])
    layer.record(["browser.open", "browser.type"])
    assert layer.successors("set_volume") == {"get_system_status": 2}
    assert layer.count() == 2
    hits = layer.recall("what usually follows set_volume", limit=3)
    assert hits and hits[0].layer == "pattern" and hits[0].kind == "pattern"
    assert hits[0].key == "set_volume" and hits[0].value == "get_system_status"
    assert "2 of 2" in hits[0].text


def test_procedural_matches_dotted_tool_names(tmp_path) -> None:
    layer = ProceduralLayer(_pattern_log(tmp_path))
    layer.record(["browser.open", "browser.type"])
    assert layer.recall("after browser.open what happens", limit=3)
    assert layer.recall("unrelated question about the weather", limit=3) == []


def test_procedural_is_read_only_and_reports_its_store(tmp_path) -> None:
    layer = ProceduralLayer(_pattern_log(tmp_path))
    layer.record(["a", "b"])
    described = layer.describe()
    assert "PatternStore" in described["store"]
    assert "read-only in Phase 8" in described["detail"]
    assert layer.top_pairs(limit=5) == [{"from": "a", "to": "b", "count": 1}]


def test_procedural_without_a_store_is_quiet() -> None:
    layer = ProceduralLayer(None)
    assert layer.available() is False and layer.count() == 0
    assert layer.record(["a", "b"]) == 0 and layer.successors("a") == {}
    assert layer.recall("a") == [] and layer.top_pairs() == []


def test_token_overlap_is_symmetric_and_bounded() -> None:
    assert token_overlap("", "anything") == 0.0
    assert token_overlap("open gmail", "open gmail") == 1.0
    score = token_overlap("open gmail now", "open gmail")
    assert 0.0 < score < 1.0
    assert score == token_overlap("open gmail", "open gmail now")
    assert LAYER_NAMES == ("working", "episodic", "semantic", "preference", "pattern")


# ── hybrid retrieval ──────────────────────────────────────────────────────────


def test_blend_applies_layer_weights() -> None:
    ranked = blend(
        [("semantic", _hit("fact", "semantic", 1.0)), ("working", _hit("turn", "working", 1.0))],
        weights={"semantic": 0.8, "working": 1.0},
        limit=5,
    )
    assert [hit.layer for hit in ranked] == ["working", "semantic"]
    assert ranked[0].score == 1.0 and ranked[1].score == 0.8


def test_blend_collapses_duplicates_keeping_the_best_score() -> None:
    ranked = blend(
        [
            ("semantic", _hit("Nilanjan drinks coffee", "semantic", 0.4)),
            ("episodic", _hit("nilanjan   drinks COFFEE", "episodic", 0.9)),
        ],
        weights={"semantic": 1.0, "episodic": 1.0},
        limit=5,
    )
    assert len(ranked) == 1 and ranked[0].score == 0.9 and ranked[0].layer == "episodic"


def test_blend_caps_a_loud_layer_then_fills_the_rest() -> None:
    loud = [("working", _hit(f"turn {index}", "working", 0.9 - index * 0.01)) for index in range(5)]
    quiet = [("semantic", _hit("a fact", "semantic", 0.5))]
    ranked = blend(loud + quiet, weights={"working": 1.0, "semantic": 0.8}, limit=4)
    assert len(ranked) == 4
    # the per-layer cap is ceil(4 / 2) = 2, so the quiet layer gets in at rank 3; the
    # remaining slot is then filled from the deferred working hits instead of going waste
    assert [hit.layer for hit in ranked] == ["working", "working", "semantic", "working"]


def test_blend_clamps_relevance_and_never_exceeds_the_range() -> None:
    ranked = blend(
        [("semantic", _hit("negative relevance", "semantic", -0.5)), ("working", _hit("full", "working", 1.0))],
        weights={"semantic": 1.0, "working": 1.0},
        limit=5,
    )
    assert all(-1.0 <= hit.score <= 1.0 for hit in ranked)
    by_layer = {hit.layer: hit.score for hit in ranked}
    assert by_layer["semantic"] == 0.0          # a negative relevance is clamped, not rewarded
    assert by_layer["working"] == 1.0           # …and a weight can never push a score past 1


async def test_hybrid_retrieve_asks_every_layer() -> None:
    layers = {
        "working": QuietLayer("working", [_hit("turn", "working", 0.5, kind="turn")]),
        "semantic": QuietLayer("semantic", [_hit("fact", "semantic", 0.5)]),
        "preference": QuietLayer("preference", [_hit("pref", "preference", 0.5, kind="preference")]),
    }
    hits, trace = await hybrid_retrieve(layers, "coffee", limit=6, per_layer=3, weights={
        "working": 1.0, "semantic": 0.8, "preference": 0.95,
    })
    assert len(hits) == 3 and trace.candidates == 3 and trace["returned"] == 3
    assert all(layer.asked == ["coffee"] for layer in layers.values())
    assert trace["timeouts"] == [] and trace["errors"] == []
    assert [hit.layer for hit in hits] == ["working", "preference", "semantic"]


async def test_hybrid_retrieve_reports_a_timeout_instead_of_hanging() -> None:
    layers = {"episodic": SlowLayer(), "semantic": QuietLayer("semantic", [_hit("fact", "semantic", 0.7)])}
    hits, trace = await hybrid_retrieve(layers, "coffee", limit=5, per_layer=3, timeout_s=0.1,
                                        weights={"episodic": 0.85, "semantic": 0.8})
    assert trace["timeouts"] == ["episodic"]
    assert [hit.layer for hit in hits] == ["semantic"]


async def test_hybrid_retrieve_survives_a_broken_layer() -> None:
    layers = {"semantic": BoomLayer(), "working": QuietLayer("working", [_hit("turn", "working", 0.6)])}
    hits, trace = await hybrid_retrieve(layers, "coffee", limit=5, per_layer=3,
                                        weights={"semantic": 0.8, "working": 1.0})
    assert len(hits) == 1 and hits[0].layer == "working"
    assert len(trace["errors"]) == 1 and "semantic" in trace["errors"][0]


async def test_hybrid_retrieve_ignores_an_empty_query() -> None:
    layer = QuietLayer("semantic", [_hit("fact", "semantic", 0.9)])
    hits, trace = await hybrid_retrieve({"semantic": layer}, "   ", limit=5)
    assert hits == [] and layer.asked == [] and trace["returned"] == 0


async def test_hybrid_retrieve_gives_the_working_layer_the_session() -> None:
    class SessionSpy:
        name = "working"
        seen: dict[str, Any] = {}

        def recall(self, query: str, session_id: str = "", *, limit: int = 5) -> list[MemoryHit]:
            SessionSpy.seen = {"query": query, "session_id": session_id, "limit": limit}
            return []

    await hybrid_retrieve({"working": SessionSpy()}, "coffee", limit=4, session_id="s-9", per_layer=2)
    assert SessionSpy.seen == {"query": "coffee", "session_id": "s-9", "limit": 2}


def test_retrieval_trace_reads_nicely() -> None:
    trace = RetrievalTrace(candidates=7, returned=3)
    assert trace.candidates == 7 and trace.returned == 3
    assert RetrievalTrace().candidates == 0


# ── manager: state ────────────────────────────────────────────────────────────


def test_layer_of_kind_maps_every_alias() -> None:
    assert layer_of_kind("fact") == "semantic"
    assert layer_of_kind("PREFERENCE") == "preference"
    assert layer_of_kind("episode") == "episodic"
    assert layer_of_kind("working") == "working"
    assert layer_of_kind("pattern") == "pattern"
    assert layer_of_kind("something-else") == "semantic"
    assert layer_of_kind(None) == "semantic"


async def test_manager_startup_opens_the_existing_store(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path), bus=bus)
    assert manager.store is None
    await manager.startup()
    try:
        assert manager.store is not None and manager.available() is True
        assert (tmp_path / "memory.db").exists()
        assert manager.episodic.available() and manager.procedural.available()
        assert "startup" in [payload.get("action") for payload in _payloads(bus, "memory.updated")]
    finally:
        await manager.aclose()


def test_manager_reports_which_layers_are_available(memory: MemoryManager) -> None:
    described = memory.describe()
    assert described["name"] == "layered" and described["enabled"] is True
    assert set(described["layers"]) == set(LAYER_NAMES)
    assert described["limits"]["retrieval"] == 8
    assert described["long_term_store"] is True
    health = memory.health()
    assert health["status"] == "ok" and health["detail"]["missing"] == []


def test_manager_weights_come_from_settings(tmp_path, bus) -> None:
    manager = build_memory(
        _settings(tmp_path, memory={"weight_working": 0.5, "weight_semantic": 1.0}),
        bus=bus,
        store=_store(tmp_path),
        own_store=True,
    )
    try:
        assert manager.weights == {
            "working": 0.5, "preference": 0.95, "episodic": 0.85, "semantic": 1.0, "pattern": 0.6,
        }
    finally:
        manager.close()


def test_manager_health_is_off_when_disabled(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path, memory={"enabled": False}), bus=bus)
    try:
        assert manager.available() is False
        assert manager.health()["status"] == "off"
        assert "STAR_MEMORY_ENABLED" in json.dumps(manager.health())
    finally:
        manager.close()


def test_disabled_manager_touches_no_disk(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path, memory={"enabled": False}), bus=bus)
    try:
        assert manager.store is None and manager.episodes_store is None and manager.pattern_store is None
        assert not (tmp_path / "memory.db").exists()
        assert not (tmp_path / "episodes.jsonl").exists()
        assert not (tmp_path / "patterns.jsonl").exists()
    finally:
        manager.close()


def test_manager_health_degrades_when_a_store_is_missing(tmp_path, bus, monkeypatch) -> None:
    manager = _bare_manager(tmp_path, bus, monkeypatch)
    try:
        health = manager.health()
        assert health["status"] == "degraded"
        assert sorted(health["detail"]["missing"]) == ["episodic", "pattern", "preference", "semantic"]
        assert manager.available() is False
    finally:
        manager.close()


def test_manager_adopts_the_brain_pattern_store(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path), bus=bus, store=_store(tmp_path), own_store=True)
    try:
        shared = _pattern_log(tmp_path)
        shared.record(["set_volume", "get_system_status"])
        assert manager.attach_patterns(shared) is True
        assert manager.procedural.patterns is shared
        assert manager.layers["pattern"] is manager.procedural
        assert manager.attach_patterns(None) is False
    finally:
        manager.close()


def test_manager_attach_store_rebinds_the_long_term_layers(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path), bus=bus, store=None, own_store=False)
    try:
        assert manager.semantic.available() is False
        handle = _store(tmp_path)
        assert manager.attach_store(handle) is True
        assert manager.semantic.store is handle and manager.prefs.store is handle
        assert manager.episodic.recall("x") == []
        handle.close()
        assert manager.attach_store(None) is False
    finally:
        manager.close()


def test_manager_aclose_is_idempotent(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path), bus=bus, store=_store(tmp_path), own_store=True)
    manager.close()
    manager.close()                     # no error, no double-free
    assert manager.available() is False


# ── manager: writes ───────────────────────────────────────────────────────────


async def test_manager_remember_routes_each_kind_to_its_layer(started: MemoryManager, bus: StarEventBus) -> None:
    fact = await started.remember("Nilanjan drinks black coffee", kind="fact")
    preference = await started.remember("bn", kind="preference", key="reply_language", value="bn")
    episode = await started.remember("opened gmail and sent the report", kind="episode")
    turn = await started.remember("a scratch note", kind="working")
    pattern = await started.remember("tool → tool", kind="pattern")

    assert fact["ok"] and fact["layer"] == "semantic" and fact["memory_id"] >= 1
    assert preference["ok"] and preference["layer"] == "preference" and preference["key"] == "reply_language"
    assert episode["ok"] and episode["layer"] == "episodic" and episode["episode_id"].startswith("ep_")
    assert turn["ok"] and turn["layer"] == "working" and turn["turns"] >= 1
    assert pattern["ok"] is False and "Phase 9" in pattern["error"]      # patterns are learned, not typed

    layers = {payload.get("layer") for payload in _payloads(bus, "memory.updated")}
    assert {"semantic", "preference", "episodic", "working"} <= layers
    assert started.stats["writes"] == 4


async def test_manager_remember_derives_a_preference_key_when_none_is_given(started: MemoryManager) -> None:
    result = await started.remember("ami bengali te kotha bolte chai", kind="preference")
    assert result["ok"] and result["key"] == "ami_bengali_te_kotha"
    assert started.prefs.get("ami_bengali_te_kotha") == "ami bengali te kotha bolte chai"


async def test_manager_remember_falls_back_to_semantic_for_an_unknown_kind(started: MemoryManager) -> None:
    result = await started.remember("the wifi name is Star-5G", kind="totally-new-kind")
    assert result["ok"] and result["layer"] == "semantic"


async def test_manager_remember_refuses_empty_text(started: MemoryManager) -> None:
    assert (await started.remember("   "))["ok"] is False
    assert "text" in (await started.remember(""))["error"]


async def test_manager_remember_refuses_when_disabled(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path, memory={"enabled": False}), bus=bus)
    try:
        result = await manager.remember("anything", kind="fact")
        assert result["ok"] is False and "disabled" in result["error"]
    finally:
        manager.close()


async def test_manager_reports_a_failing_store_instead_of_raising(tmp_path, bus) -> None:
    class BrokenStore:
        def remember(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("database is locked")

        def count(self) -> int:
            raise OSError("database is locked")

    manager = build_memory(_settings(tmp_path), bus=bus, store=BrokenStore(), own_store=False)
    try:
        result = await manager.remember("a fact", kind="fact")
        assert result["ok"] is False and "database is locked" in result["error"]
        assert "memory.failed" in _kinds(bus)
        assert manager.stats["failures"] >= 1
    finally:
        manager.close()


async def test_manager_set_preference_requires_key_and_value(started: MemoryManager) -> None:
    assert (await started.set_preference("", "bn"))["ok"] is False
    assert (await started.set_preference("language", "  "))["ok"] is False
    ok = await started.set_preference("language", "bn")
    assert ok["ok"] and ok["value"] == "bn" and started.stats["preferences"] == 1


async def test_manager_record_episode_leaves_a_working_trace(started: MemoryManager) -> None:
    result = await started.record_episode(
        "open example.com and read the headline",
        steps=[{"tool": "browser.open", "arguments": {"url": "https://example.com"}, "ok": True}],
        succeeded=True,
        session_id="s1",
        agent="browser",
    )
    assert result["ok"] and result["steps"] == 1 and result["succeeded"] is True
    turns = started.working_turns("s1")
    assert turns and turns[-1].startswith("browser: open example.com") and turns[-1].endswith("done")
    assert started.episodes(limit=3)[0]["goal"].startswith("open example.com")


async def test_manager_record_episode_needs_a_goal_or_a_store(
    started: MemoryManager, tmp_path, bus, monkeypatch
) -> None:
    assert (await started.record_episode("  "))["ok"] is False
    empty = _bare_manager(tmp_path, bus, monkeypatch)
    try:
        result = await empty.record_episode("a goal")
        assert result["ok"] is False and "no episodic store" in result["error"]
    finally:
        empty.close()


async def test_manager_forget_deletes_one_record(started: MemoryManager, bus: StarEventBus) -> None:
    stored = await started.remember("the office wifi password is hunter2", kind="fact")
    hits, _ = started.search("wifi password")
    assert hits
    result = await started.forget(stored["memory_id"])
    assert result["ok"] and result["removed"] is True
    assert started.search("wifi password")[0] == []
    assert [payload.get("action") for payload in _payloads(bus, "memory.updated")].count("forget") == 1


async def test_manager_forget_rejects_a_bad_id(started: MemoryManager) -> None:
    assert (await started.forget("not-an-id"))["ok"] is False
    missing = await started.forget(999999)
    assert missing["ok"] is False and missing["removed"] is False


async def test_manager_forget_session_clears_working_memory(started: MemoryManager) -> None:
    started.remember_turn("s1", "hello")
    result = started.forget_session("s1")
    assert result["ok"] and result["removed"] == 1
    assert started.working_turns("s1") == []


# ── manager: retrieval ────────────────────────────────────────────────────────


async def test_manager_retrieve_blends_every_layer(started: MemoryManager, bus: StarEventBus) -> None:
    await started.remember("Nilanjan drinks black coffee", kind="fact")
    await started.set_preference("reply_language", "bn")
    await started.record_episode("sent the weekly report by email", steps=[{"tool": "browser.open", "ok": True}])
    started.remember_turn("s1", "did the coffee and the report go well?")
    started.procedural.record(["browser.open", "browser.type"])

    hits = await started.retrieve("coffee report browser.open language", limit=10, session_id="s1")
    layers = {hit.layer for hit in hits}
    assert {"working", "semantic", "preference", "episodic", "pattern"} <= layers
    assert all(-1.0 <= hit.score <= 1.0 for hit in hits)
    assert hits == sorted(hits, key=lambda hit: -hit.score)

    trace = started.last_trace
    assert trace["returned"] == len(hits) and trace.candidates >= len(hits)
    retrieved = _payloads(bus, "memory.retrieved")
    assert retrieved and retrieved[-1]["returned"] == len(hits)
    assert retrieved[-1]["candidates"] == trace.candidates


async def test_manager_retrieve_survives_a_broken_layer(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path), bus=bus, store=_store(tmp_path), own_store=True)
    try:
        await manager.remember("a fact about tea", kind="fact")
        manager.layers["semantic"] = BoomLayer()
        hits = await manager.retrieve("tea", limit=5)
        assert isinstance(hits, list)                       # no exception escapes
        assert manager.last_trace["errors"] and manager.stats["failures"] >= 1
    finally:
        manager.close()


async def test_manager_retrieve_is_empty_when_disabled(tmp_path, bus) -> None:
    manager = build_memory(_settings(tmp_path, memory={"enabled": False}), bus=bus)
    try:
        assert await manager.retrieve("anything") == []
        assert "disabled" in manager.last_trace["error"]
    finally:
        manager.close()


async def test_manager_digest_mixes_the_store_block_with_the_layers(started: MemoryManager) -> None:
    await started.remember("Nilanjan drinks black coffee", kind="fact")
    await started.set_preference("coffee", "black, no sugar")
    started.remember_turn("s1", "coffee please")
    await started.retrieve("coffee", limit=6, session_id="s1")     # primes the cache digest() reuses
    digest = await started.digest("coffee", limit=6)
    assert digest.startswith("Relevant things you know about Tester:")   # the store's own block
    assert "Nilanjan drinks black coffee" in digest
    assert "black, no sugar" in digest                             # the preference reached the prompt
    assert "(working) coffee please" in digest                     # …plus what the block cannot know
    assert digest.count("black, no sugar") == 1                    # never repeated twice


async def test_manager_digest_falls_back_to_the_layers(tmp_path, bus, monkeypatch) -> None:
    manager = _bare_manager(tmp_path, bus, monkeypatch)
    try:
        manager.remember_turn("s1", "coffee please")
        await manager.retrieve("coffee", limit=4, session_id="s1")
        digest = await manager.digest("coffee", limit=4)
        assert digest.startswith("What Star remembers:")
        assert "(working) coffee please" in digest
    finally:
        manager.close()


async def test_manager_digest_is_empty_without_a_query_or_when_disabled(started: MemoryManager, tmp_path, bus) -> None:
    assert await started.digest("   ") == ""
    disabled = build_memory(_settings(tmp_path, memory={"enabled": False}), bus=bus)
    try:
        assert await disabled.digest("coffee") == ""
    finally:
        disabled.close()


async def test_manager_search_works_from_a_running_loop(started: MemoryManager) -> None:
    """The gateway calls the sync snapshot from an async handler — it must not deadlock."""
    await started.remember("Nilanjan drinks black coffee", kind="fact")
    hits, trace = started.search("coffee", limit=5)          # called on the event-loop thread
    assert hits and hits[0].layer == "semantic"
    assert trace["returned"] == len(hits)


def test_manager_search_works_without_a_loop(memory: MemoryManager) -> None:
    memory.remember_now("the printer is on the second floor", kind="fact")
    hits, trace = memory.search("printer floor", limit=5)
    assert hits and "printer" in hits[0].text and trace["returned"] == len(hits)


def test_manager_search_from_a_worker_thread_without_a_loop(memory: MemoryManager) -> None:
    memory.remember_now("the printer is on the second floor", kind="fact")
    outcome: dict[str, Any] = {}

    def worker() -> None:
        hits, _ = memory.search("printer", limit=3)
        outcome["hits"] = len(hits)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive() and outcome["hits"] >= 1


def test_manager_sync_writes_mirror_the_async_ones(memory: MemoryManager) -> None:
    assert memory.remember_now("a fact", kind="fact")["ok"] is True
    assert memory.set_preference_now("theme", "dark")["ok"] is True
    assert memory.record_episode_now("did a thing", steps=[{"tool": "x", "ok": True}])["ok"] is True
    assert memory.forget_now(1)["removed"] is True
    assert memory.forget_now("nope")["ok"] is False


# ── manager: snapshot (the console contract) ──────────────────────────────────


def test_manager_snapshot_matches_the_console_contract(memory: MemoryManager) -> None:
    memory.remember_now("Nilanjan drinks black coffee", kind="fact")
    memory.set_preference_now("reply_language", "bn")
    memory.record_episode_now("sent the report", steps=[{"tool": "browser.open", "ok": True}])
    memory.remember_turn("test-session", "coffee?")

    snapshot = memory.snapshot("coffee", limit=10)
    assert snapshot["ok"] is True and snapshot["query"] == "coffee"
    assert set(snapshot["layers"]) == set(LAYER_NAMES)
    for name, layer in snapshot["layers"].items():
        assert {"count", "store", "detail"} <= set(layer), name
        assert isinstance(layer["count"], int) and layer["store"]
    assert snapshot["layers"]["semantic"]["count"] == 1
    assert snapshot["layers"]["preference"]["count"] == 1
    assert snapshot["layers"]["episodic"]["count"] == 1
    assert snapshot["preferences"][0]["key"] == "reply_language"
    assert snapshot["preferences"][0]["value"] == "bn"
    assert snapshot["preferences"][0]["timestamp"]
    retrieved = snapshot["retrieved"]
    assert retrieved and {"layer", "text", "score", "kind"} <= set(retrieved[0])
    assert snapshot["episodes"][0]["goal"] == "sent the report"
    assert "coffee?" in snapshot["working"]
    assert snapshot["weights"]["working"] == 1.0
    assert snapshot["trace"]["returned"] == len(retrieved)
    assert snapshot["store"].endswith("memory.db")


def test_manager_snapshot_without_a_query_reports_the_layers_only(memory: MemoryManager) -> None:
    memory.remember_now("a fact", kind="fact")
    snapshot = memory.snapshot()
    assert snapshot["ok"] is True and snapshot["query"] is None
    assert snapshot["retrieved"] == []
    assert snapshot["layers"]["semantic"]["count"] == 1


async def test_manager_snapshot_async_matches_the_sync_snapshot(started: MemoryManager) -> None:
    await started.remember("Nilanjan drinks black coffee", kind="fact")
    await started.set_preference("reply_language", "bn")
    async_snapshot = await started.snapshot_async("coffee", limit=8)
    sync_snapshot = started.snapshot("coffee", limit=8)
    assert async_snapshot["ok"] is sync_snapshot["ok"] is True
    assert [hit["text"] for hit in async_snapshot["retrieved"]] == [
        hit["text"] for hit in sync_snapshot["retrieved"]
    ]
    assert async_snapshot["preferences"] == sync_snapshot["preferences"]


def test_manager_views_never_raise_on_a_broken_store(tmp_path, bus) -> None:
    class BrokenStore:
        def recall_recent(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("disk on fire")

        def latest_by_key(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("disk on fire")

        def count(self) -> int:
            raise OSError("disk on fire")

    manager = build_memory(_settings(tmp_path), bus=bus, store=BrokenStore(), own_store=False)
    try:
        assert manager.preferences() == [] and manager.facts() == []
        snapshot = manager.snapshot("anything")
        assert snapshot["ok"] is True                       # degraded, not broken
        assert snapshot["layers"]["semantic"]["count"] == 0
    finally:
        manager.close()


# ── manager: events ───────────────────────────────────────────────────────────


async def test_manager_publishes_official_event_kinds(started: MemoryManager, bus: StarEventBus) -> None:
    await started.remember("a fact", kind="fact")
    await started.retrieve("fact")
    kinds = set(_kinds(bus))
    assert {"memory.updated", "memory.retrieved"} <= kinds
    assert kinds <= EVENT_KINDS                             # nothing is flagged _unknown_kind
    for event in bus.history(limit=300):
        if event.kind.startswith("memory."):
            assert "_unknown_kind" not in event.payload
            assert event.phase.value == "memory"
            assert event.session_id                            # never None on the wire


async def test_manager_events_carry_the_layer_and_the_trace(started: MemoryManager, bus: StarEventBus) -> None:
    await started.remember("a fact about tea", kind="fact")
    await started.retrieve("tea", limit=4)
    updated = _payloads(bus, "memory.updated")[-1]
    assert updated["layer"] == "semantic" and updated["action"] == "remember"
    assert updated["memory_kind"] == "fact" and updated["memory_id"] >= 1
    retrieved = _payloads(bus, "memory.retrieved")[-1]
    assert retrieved["layer"] == "semantic" and retrieved["returned"] >= 1
    assert retrieved["latency_ms"] >= 0 and retrieved["query"] == "tea"


# ── tools ─────────────────────────────────────────────────────────────────────


def _executor(tmp_path, manager: MemoryManager | None, *, dry_run: bool = False, bus: StarEventBus | None = None):
    cfg = _settings(tmp_path, dry_run=dry_run)
    event_bus = bus or StarEventBus(history_size=300)
    registry = StarToolRegistry(cfg, bus=event_bus, import_legacy=False)
    kit = register_memory_tools(registry, cfg, bus=event_bus, manager=manager)
    audit = AuditLog(tmp_path / "audit.jsonl", bus=event_bus)
    confirmations = ConfirmationStore(cfg, bus=event_bus)
    permissions = PermissionEngine(cfg, bus=event_bus, confirmations=confirmations)
    executor = ToolExecutor(
        cfg, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=event_bus
    )
    return SimpleNamespace(
        executor=executor, registry=registry, kit=kit, audit=audit, bus=event_bus,
        confirmations=confirmations, settings=cfg,
    )


def test_register_memory_tools_adds_seven_typed_specs(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    names = [name for name in rig.registry.names() if name.startswith("memory_")]
    assert sorted(names) == [
        "memory_forget",
        "memory_list_episodes",
        "memory_list_preferences",
        "memory_preference_set",
        "memory_read_working",
        "memory_recall",
        "memory_status",
    ]
    for name in names:
        spec = rig.registry.get(name)
        assert spec is not None and spec.origin == "star2"
        assert spec.agent.value == "conversation" and spec.category.value == "memory"
        assert spec.module == "Backend.star.memory.tools"
        assert "phase8" in spec.tags and spec.callable is True
    assert rig.registry.get("memory_recall").risk == "low"
    assert rig.registry.get("memory_preference_set").risk == "medium"
    forget = rig.registry.get("memory_forget")
    assert forget.risk == "high" and forget.reversible is False and forget.idempotent is False


def test_register_memory_tools_is_idempotent(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    register_memory_tools(rig.registry, rig.settings, bus=rig.bus, manager=memory)
    assert len([name for name in rig.registry.names() if name.startswith("memory_")]) == 7


async def test_memory_recall_blends_layers_through_the_executor(tmp_path, memory: MemoryManager) -> None:
    memory.remember_now("Nilanjan drinks black coffee", kind="fact")
    memory.set_preference_now("reply_language", "bn")
    memory.record_episode_now("sent the weekly report", steps=[{"tool": "browser.open", "ok": True}])
    memory.remember_turn("test-session", "coffee and the report")
    rig = _executor(tmp_path, memory)

    result = await rig.executor.call("memory_recall", {"query": "coffee report language", "limit": 10},
                                     session_id="s1")
    assert result.ok is True and result.decision == "executed" and result.risk == "low"
    data = result.data
    assert data["count"] == len(data["hits"]) >= 3
    assert set(data["layers_used"]) >= {"semantic", "preference", "episodic", "working"}
    assert data["weights"]["working"] == 1.0
    assert "read-only" in data["note"]
    assert "memory hit(s) across" in result.output
    assert rig.kit.stats["ok"] == 1


async def test_memory_recall_reports_an_empty_search_honestly(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    result = await rig.executor.call("memory_recall", {"query": "zzz nothing matches zzz"}, session_id="s1")
    assert result.ok is True and result.data["count"] == 0
    assert "Nothing in memory matched" in result.output
    assert rig.kit.stats["empty"] == 1


async def test_memory_recall_requires_a_query(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    result = await rig.executor.call("memory_recall", {}, session_id="s1")
    assert result.ok is False and result.decision == "invalid_args"
    assert "query" in (result.error or "")
    blank = await rig.executor.call("memory_recall", {"query": "   "}, session_id="s1")
    assert blank.ok is False and "query is required" in (blank.error or "")


async def test_memory_read_tools_report_their_layers(tmp_path, memory: MemoryManager) -> None:
    memory.set_preference_now("reply_language", "bn")
    memory.record_episode_now("sent the weekly report", steps=[{"tool": "browser.open", "ok": True}], succeeded=False)
    memory.remember_turn("s1", "hello star")
    rig = _executor(tmp_path, memory)

    preferences = await rig.executor.call("memory_list_preferences", {}, session_id="s1")
    assert preferences.ok and preferences.data["preferences"][0]["value"] == "bn"

    episodes = await rig.executor.call("memory_list_episodes", {}, session_id="s1")
    assert episodes.ok and episodes.data["count"] == 1
    assert episodes.data["failed"] == 1 and "succeeded episodes" in episodes.data["note"]

    working = await rig.executor.call("memory_read_working", {"session_id": "s1"}, session_id="s1")
    assert working.ok and working.data["turns"] == ["hello star"]
    assert working.data["capacity"]["turns"] == 8

    status = await rig.executor.call("memory_status", {}, session_id="s1")
    assert status.ok and status.data["status"] == "ok"
    assert set(status.data["layers"]) == set(LAYER_NAMES)
    assert status.data["no_second_store"] is True and status.data["long_term_store"] is True
    assert status.data["limits"]["timeout_s"] == 4.0


async def test_memory_preference_set_stores_and_is_medium_risk(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    result = await rig.executor.call("memory_preference_set", {"key": "theme", "value": "dark"}, session_id="s1")
    assert result.ok is True and result.risk == "medium" and result.decision == "executed"
    assert "theme = dark" in result.output
    assert memory.prefs.get("theme") == "dark"

    blank = await rig.executor.call("memory_preference_set", {"key": "", "value": ""}, session_id="s1")
    assert blank.ok is False and blank.decision == "invalid_args"


async def test_memory_forget_needs_confirmation_then_really_deletes(tmp_path, memory: MemoryManager) -> None:
    stored = memory.remember_now("the office wifi password is hunter2", kind="fact")
    rig = _executor(tmp_path, memory)

    blocked = await rig.executor.call("memory_forget", {"memory_id": stored["memory_id"]}, session_id="s1")
    assert blocked.ok is False and blocked.decision == "needs_confirmation" and blocked.risk == "high"
    assert blocked.confirmation_id and memory.search("wifi password")[0]      # still there

    approved = await rig.executor.execute_confirmed(rig.confirmations.get(blocked.confirmation_id))
    assert approved["ok"] is True and approved["removed"] is True
    assert memory.search("wifi password")[0] == []
    entries = [(entry["tool"], entry["decision"], entry["risk"]) for entry in rig.audit.tail(limit=2)]
    assert entries == [                                     # the audit trail keeps both moments
        ("memory_forget", "needs_confirmation", "high"),
        ("memory_forget", "executed", "high"),
    ]


async def test_memory_forget_reports_an_unknown_id(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    result = await rig.executor.call(
        "memory_forget", {"memory_id": 424242}, session_id="s1", confirmation_id=None
    )
    assert result.ok is False and result.decision == "needs_confirmation"      # risk gate comes first
    rig.confirmations.mark_approved(result.confirmation_id, note="test")
    approved = await rig.executor.execute_confirmed(rig.confirmations.get(result.confirmation_id))
    assert approved["ok"] is False and "Nothing was deleted" in approved["output"]


async def test_memory_tools_are_simulated_in_dry_run(tmp_path, memory: MemoryManager) -> None:
    memory.remember_now("a fact", kind="fact")
    rig = _executor(tmp_path, memory, dry_run=True)
    recalled = await rig.executor.call("memory_recall", {"query": "fact"}, session_id="s1")
    assert recalled.ok is True and recalled.decision == "simulated" and recalled.dry_run is True
    assert recalled.data["simulated"] is True

    written = await rig.executor.call("memory_preference_set", {"key": "theme", "value": "dark"}, session_id="s1")
    assert written.decision == "simulated" and memory.prefs.get("theme") is None   # nothing was stored


def test_memory_tools_answer_honestly_without_a_manager(tmp_path) -> None:
    kit = MemoryToolkit(None, _settings(tmp_path))
    recalled = kit.recall("anything")
    assert recalled["ok"] is False and "not wired" in recalled["error"]
    assert "Memory is not available" in recalled["output"]
    for result in (
        kit.list_preferences(), kit.list_episodes(), kit.read_working(), kit.status(),
        kit.set_preference("a", "b"), kit.forget(1),
    ):
        assert result["ok"] is False and "not wired" in result["error"]
    assert kit.stats["no_manager"] == 7


def test_memory_toolkit_bounds_its_limits(tmp_path, memory: MemoryManager) -> None:
    kit = MemoryToolkit(memory, _settings(tmp_path))
    assert kit._bound("5", 8, 40) == 5
    assert kit._bound("nonsense", 8, 40) == 8
    assert kit._bound(9999, 8, 40) == 40
    assert kit._bound(-3, 8, 40) == 1
    assert kit.recall("x", limit=9999)["ok"] is True


async def test_memory_tools_are_refused_while_the_emergency_stop_is_active(tmp_path, memory: MemoryManager) -> None:
    rig = _executor(tmp_path, memory)
    rig.executor.permissions.bind_stop_gate(lambda: True)
    result = await rig.executor.call("memory_recall", {"query": "coffee"}, session_id="s1")
    assert result.ok is False and result.decision == "denied"
    assert "emergency stop" in (result.error or "")


# ── application wiring ────────────────────────────────────────────────────────


class FakeBrain:
    """Deterministic brain double: returns a plan whose task really ran a tool."""

    name = "fake"

    def __init__(self, *, steps: list[dict[str, Any]] | None = None) -> None:
        self.steps = steps if steps is not None else [
            {"call_id": "call_1", "tool": "set_volume", "arguments": {"level": 40}, "state": "done"}
        ]
        self.context_builder = FakeScratch()
        self.predictor = SimpleNamespace(store=None)
        self.store = None
        self.retriever = None

    async def startup(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def handle(self, text: str, *, session_id: str | None = None, language: str | None = None,
                     source: str = "text") -> dict[str, Any]:
        task = {
            "task_id": "task_1", "goal": text, "agent": "system", "state": "done",
            "risk": "medium", "steps": self.steps, "summary": "done",
        }
        if self.context_builder is not None:
            self.context_builder.remember_turn(session_id or "test-session", text)
        return {
            "ok": True, "response": f"fake:{text}", "source": "fake_brain", "session_id": session_id,
            "language": language or "bn", "tasks": [task],
            "plan": {"plan_id": "plan_1", "intent": "demo", "tasks": [task]},
        }


async def _app(tmp_path, *, brain: Any = None, memory_overrides: dict[str, Any] | None = None, **slots: Any):
    from Backend.star.main import build_application

    cfg = _settings(tmp_path, memory=memory_overrides)
    app = build_application(cfg, **({} if brain is None else {"brain": brain}), **slots)
    await app.startup()
    return app


async def test_build_application_wires_memory_as_the_brain_retriever(tmp_path) -> None:
    app = await _app(tmp_path)
    try:
        assert app.memory is not None and app.memory.name == "layered"
        assert "memory" in app.capabilities()
        assert app.health()["checks"]["memory"]["status"] == "ok"
        assert app.brain.retriever is app.memory                 # memory is retrieved before planning
        assert app.brain.store is app.memory.store               # …over the very same connection
        assert app.memory.store is not None
    finally:
        await app.aclose()


async def test_build_application_shares_the_brain_scratch_and_pattern_store(tmp_path) -> None:
    app = await _app(tmp_path)
    try:
        assert app.memory.working.delegated is True
        assert app.memory.working._provider is app.brain.context_builder
        assert app.memory.procedural.patterns is app.brain.predictor.store
    finally:
        await app.aclose()


async def test_build_application_registers_the_memory_tools(tmp_path) -> None:
    app = await _app(tmp_path)
    try:
        names = {tool["name"] for tool in app.tools()}
        assert {"memory_recall", "memory_status", "memory_forget", "memory_preference_set"} <= names
        # the legacy memory tools are wrapped, never replaced by the Phase 8 ones
        for legacy in ("remember_fact", "recall_memory"):
            spec = app.tool_registry.get(legacy)
            assert spec is None or spec.origin == "legacy"
    finally:
        await app.aclose()


async def test_app_remember_and_memory_snapshot_round_trip(tmp_path) -> None:
    app = await _app(tmp_path)
    try:
        stored = await app.remember("Nilanjan drinks black coffee", kind="fact")
        assert stored["ok"] is True and stored["layer"] == "semantic"
        preference = await app.remember("bn", kind="preference", key="reply_language", value="bn")
        assert preference["ok"] is True

        snapshot = await app.memory_snapshot_async("coffee", limit=8)
        assert snapshot["ok"] is True and snapshot["retrieved"]
        assert snapshot["preferences"][0]["key"] == "reply_language"
        assert snapshot["layers"]["semantic"]["count"] == 1
        assert app.memory_snapshot("coffee", limit=8)["query"] == "coffee"
    finally:
        await app.aclose()


async def test_chat_keeps_working_memory_and_skips_small_talk_episodes(tmp_path) -> None:
    brain = FakeBrain(steps=[])
    app = await _app(tmp_path, brain=brain)
    try:
        result = await app.chat("hello star", session_id="s1")
        assert result["ok"] is True
        turns = app.memory.working_turns("s1")
        assert "hello star" in turns                             # the brain's own scratch
        assert any(turn.startswith("star: fake:hello star") for turn in turns)
        assert app.memory.episodes(limit=5) == []                # nothing was done ⇒ no episode
    finally:
        await app.aclose()


async def test_chat_records_an_episode_when_a_tool_ran(tmp_path) -> None:
    app = await _app(tmp_path, brain=FakeBrain())
    try:
        await app.chat("volume 40 koro", session_id="s1")
        episodes = app.memory.episodes(limit=5)
        assert len(episodes) == 1
        assert episodes[0]["goal"] == "volume 40 koro" and episodes[0]["steps"] == 1
        assert episodes[0]["succeeded"] is True
        hits = await app.memory.retrieve("volume", limit=5, session_id="s1")
        assert any(hit.layer == "episodic" for hit in hits)
    finally:
        await app.aclose()


async def test_agent_run_records_a_labelled_dry_run_episode(tmp_path) -> None:
    run = AgentRun(
        agent="browser",
        goal="open example.com",
        state="dry_run",
        dry_run=True,
        session_id="s1",
        steps=[
            AgentStep(index=0, action="open", tool="browser.open", arguments={"url": "https://example.com"},
                      decision="simulated", ok=True, expected="the page loads"),
            AgentStep(index=1, action="click", tool="browser.click", arguments={}, decision="skipped",
                      ok=False, skipped=True),
        ],
    )

    class FakeBrowser:
        """Just enough of a ``StarAgent`` for the app to register and run it."""

        name = AgentName.BROWSER
        description = "fake browser agent"
        tools: tuple[str, ...] = ()
        keywords: tuple[str, ...] = ()

        async def startup(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

        def describe(self) -> dict[str, Any]:
            return {"name": self.name.value, "fake": True}

        def score_goal(self, goal: str) -> float:
            return 1.0

        async def run(self, goal: str, *, session_id: str = "", dry_run: bool | None = None) -> AgentRun:
            return run

    app = await _app(tmp_path, browser=FakeBrowser())
    try:
        payload = await app.run_browser_goal("open example.com", session_id="s1")
        assert payload["state"] == "dry_run"
        episodes = app.memory.episodes(limit=5)
        assert len(episodes) == 1
        assert episodes[0]["goal"] == "[dry-run] open example.com"    # honest about the simulation
        assert episodes[0]["steps"] == 1 and episodes[0]["succeeded"] is True   # skipped steps are not steps
    finally:
        await app.aclose()


async def test_memory_slot_can_be_disabled_without_breaking_the_app(tmp_path) -> None:
    app = await _app(tmp_path, memory_overrides={"enabled": False})
    try:
        assert app.memory is not None and app.memory.available() is False
        assert app.health()["checks"]["memory"]["status"] == "off"
        assert (await app.remember("anything"))["ok"] is False
        snapshot = await app.memory_snapshot_async("anything")
        assert snapshot["ok"] is True and snapshot["enabled"] is False
        assert snapshot["retrieved"] == []
        result = await app.chat("hello", session_id="s1")
        assert result["ok"] is True                            # the reply still works
    finally:
        await app.aclose()


async def test_memory_survives_a_missing_long_term_store(tmp_path, bus, monkeypatch) -> None:
    manager = _bare_manager(tmp_path, bus, monkeypatch)
    try:
        assert (await manager.remember("a fact"))["ok"] is False
        assert await manager.retrieve("a fact") == []
        snapshot = manager.snapshot("a fact")
        assert snapshot["ok"] is True and snapshot["layers"]["semantic"]["available"] is False
        assert snapshot["store"] is None
    finally:
        manager.close()


# ── gateway + console ─────────────────────────────────────────────────────────


async def _get(port: int, path: str) -> tuple[int, Any]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read(200_000)
    finally:
        writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(body.decode())


async def _post(port: int, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    data = json.dumps(payload).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
        )
        await writer.drain()
        raw = await reader.read(200_000)
    finally:
        writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body.decode())


async def test_gateway_memory_endpoints(tmp_path) -> None:
    from Backend.star.gateway.server import GatewayServer

    app = await _app(tmp_path, brain=FakeBrain())
    server = GatewayServer(app, app.settings, host="127.0.0.1", port=0, bus=app.bus)
    await server.start()
    try:
        status, stored = await _post(server.port, "/api/v1/memory", {"text": "Nilanjan drinks black coffee"})
        assert status == 200 and stored["ok"] is True and stored["layer"] == "semantic"

        status, preference = await _post(
            server.port, "/api/v1/memory",
            {"text": "bn", "kind": "preference", "key": "reply_language", "value": "bn"},
        )
        assert status == 200 and preference["ok"] is True

        status, snapshot = await _get(server.port, "/api/v1/memory?q=coffee&limit=8")
        assert status == 200 and snapshot["ok"] is True and snapshot["query"] == "coffee"
        assert set(snapshot["layers"]) == set(LAYER_NAMES)
        assert snapshot["retrieved"] and snapshot["preferences"][0]["value"] == "bn"

        status, blank = await _get(server.port, "/api/v1/memory")
        assert status == 200 and blank["query"] is None and blank["retrieved"] == []

        status, bad = await _post(server.port, "/api/v1/memory", {"text": "   "})
        assert status == 422 and "text" in str(bad)
    finally:
        await server.stop()
        await app.aclose()


def test_console_memory_tab_shows_every_layer() -> None:
    from Backend.star.gateway.console import render_console

    html = render_console({"version": "test", "capabilities": ["memory"]})
    assert "renderMemory" in html and 'data-tab="memory"' in html
    for marker in ("PREFERENCES", "RETRIEVAL", "EPISODIC", "WORKING", "PROCEDURAL PATTERNS"):
        assert marker in html
    assert "STAR_MEMORY_ENABLED" in html            # the disabled state is explained in the UI


def test_console_javascript_is_syntactically_valid(tmp_path) -> None:
    """A single bad quote in the console script kills every tab — check it parses."""
    from Backend.star.gateway.console import _JS

    assert "window.confirm(\"Delete this session's files?" in _JS      # regression: it was 'session's'
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "console.js"
    script.write_text(_JS, encoding="utf-8")
    completed = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[:2000]

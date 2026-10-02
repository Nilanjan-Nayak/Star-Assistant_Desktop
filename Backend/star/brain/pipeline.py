"""StarBrain — the Phase 3 pipeline that answers a request end to end.

    UserRequest ─► Context (memory retrieved FIRST) ─► Reasoning ─► Plan
                                                        │            │
                                          Predictions ──┘            ▼
                                                          Reflection ─► result dict

Blueprint §7 Phase 3 deliverables:
  * typed schemas (``brain/schemas.py``)
  * context assembly with memory retrieval *before* planning (``brain/context.py``)
  * validated reasoning over the existing LLM providers (``brain/reasoning.py``)
  * structured plans with task states (``brain/planning.py``)
  * next-action prediction from history (``brain/prediction.py``)
  * reflection after execution (``brain/reflection.py``)
  * raw model output is never executed: everything passes the sanitiser and the
    planner; execution itself stays behind the orchestrator + policy (Phase 10/11).

The class satisfies the ``brain`` slot contract used by :mod:`Backend.star.main`
(``handle(text, *, session_id, language, source) -> dict``, ``health()``,
``describe()``, ``startup()``, ``aclose()``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from Backend.star.brain.context import ContextBuilder, MemoryRetriever, NullRetriever, StoreRetriever
from Backend.star.brain.planning import StarPlanner
from Backend.star.brain.prediction import HistoryPredictor, NullPredictor, PatternStore, Predictor
from Backend.star.brain.reflection import NullReflector, Reflector, StarReflector
from Backend.star.brain.reasoning import Reasoner, ReasoningCascade, build_reasoner
from Backend.star.brain.schemas import (
    Context,
    Plan,
    Reasoning,
    Reflection,
    TaskState,
    UserRequest,
)
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

__all__ = ["DRY_RUN_NOTES", "HONEST_NOTES", "StarBrain", "build_brain", "open_memory_store"]

_log = star_logger("brain")

#: When verification disagrees with what the tool layer claimed, Star says so.
#: A confident sentence over a failed step is worse than a short correction.
#: dry-run is the default: say so instead of implying the OS really changed.
DRY_RUN_NOTES: dict[str, str] = {
    "bn": "(dry-run মোড: আসলে কিছু বদলাইনি, শুধু দেখালাম কী হতো।)",
    "en": "(dry-run mode: nothing was really changed, this is what would happen.)",
}

HONEST_NOTES: dict[str, dict[str, str]] = {
    "bn": {
        "failed": "(তবে একটা ধাপ ব্যর্থ হয়েছে — আমি ঠিক করে আবার চেষ্টা করবো।)",
        "partial": "(কিছু ধাপ হয়েছে, বাকিটা এখনো বাকি।)",
        "blocked": "(এই কাজটা ঝুঁকিপূর্ণ, তাই নিরাপত্তার জন্য আটকানো হয়েছে।)",
        "awaiting_confirmation": "(এগোনোর আগে তোমার কনফার্মেশন দরকার।)",
    },
    "en": {
        "failed": "(But one step failed — I will fix it and retry.)",
        "partial": "(Some steps succeeded, the rest is still pending.)",
        "blocked": "(This action is risky, so it was blocked by policy.)",
        "awaiting_confirmation": "(I need your confirmation before continuing.)",
    },
}


def open_memory_store(settings: Settings) -> Any | None:
    """Open the existing SQLite memory store (``agent.memory.store.MemoryStore``).

    Returns ``None`` when it cannot be opened — the brain then runs without
    long-term memory instead of failing.
    """
    try:
        from agent.memory.store import MemoryStore

        settings.paths.memory_db.parent.mkdir(parents=True, exist_ok=True)
        return MemoryStore(path=settings.paths.memory_db, owner=settings.owner)
    except Exception as exc:  # noqa: BLE001
        _log.warning("memory store unavailable (%s) — running without long-term memory", exc)
        return None


class StarBrain:
    """Context-aware reasoning + planning + reflection."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        context_builder: ContextBuilder | None = None,
        reasoner: Reasoner | None = None,
        planner: StarPlanner | None = None,
        predictor: Predictor | None = None,
        reflector: Reflector | None = None,
        retriever: MemoryRetriever | None = None,
        executor: Any = None,                 # Phase 10 orchestrator (executes PROPOSED calls)
        store: Any = None,                    # agent MemoryStore (episodic writes)
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self.store = store
        self.executor = executor
        self.retriever = retriever if retriever is not None else (
            StoreRetriever(store) if store is not None else NullRetriever()
        )
        self.context_builder = context_builder or ContextBuilder(
            settings, retriever=self.retriever, bus=self.bus, memory_limit=settings.brain.retrieval_top_k
        )
        self.reasoner = reasoner or build_reasoner(settings, bus=self.bus)
        self.planner = planner or StarPlanner(settings, bus=self.bus)
        self.predictor = predictor or (
            HistoryPredictor(settings, store=PatternStore(settings.paths.patterns_path), bus=self.bus)
            if settings.brain.prediction_enabled
            else NullPredictor()
        )
        self.reflector = reflector or StarReflector(settings, bus=self.bus)
        self.stats: dict[str, Any] = {
            "turns": 0,
            "llm_turns": 0,
            "plans": 0,
            "tool_calls": 0,
            "failures": 0,
            "escalations": 0,
            "last_latency_ms": 0.0,
        }
        self._started = False
        self._last_plans: dict[str, list[dict[str, Any]]] = {}
        self._last_tool: dict[str, tuple[str, bool]] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        if self.store is None:
            self.store = await asyncio.to_thread(open_memory_store, self.settings)
            if self.store is not None and isinstance(self.retriever, NullRetriever):
                self.retriever = StoreRetriever(self.store)
                self.context_builder.retriever = self.retriever
        self.bus.emit(
            "agent.ready",
            phase=EventPhase.BRAIN,
            component="brain",
            reasoners=[getattr(r, "name", type(r).__name__) for r in getattr(self.reasoner, "reasoners", [])],
            memory=self.retriever.name,
            reflection=self.settings.brain.reflection_enabled,
            prediction=self.settings.brain.prediction_enabled,
        )
        _log.info("star2.brain.ready memory=%s", self.retriever.name)

    async def aclose(self) -> None:
        self._started = False
        store, self.store = self.store, None
        if store is not None:
            try:
                await asyncio.to_thread(store.close)
            except Exception as exc:  # noqa: BLE001
                _log.warning("memory store close failed: %s", exc)

    # ── the pipeline ─────────────────────────────────────────────────────────
    async def handle(
        self,
        text: str,
        *,
        session_id: str | None = None,
        language: str | None = None,
        source: str = "text",
        language_profile: dict[str, Any] | None = None,
        screen_summary: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        from Backend.star.voice.language import detect_language

        cleaned = (text or "").strip()
        profile = language_profile or detect_language(cleaned).as_dict()
        from Backend.star.tools.legacy_gate import CURRENT_CALL
        request = UserRequest(
            text=cleaned or " ",
            language=language or str(profile.get("code") or "bn"),
            language_profile=profile,
            source=source,
            session_id=session_id or self.settings.session_id,
        )
        if not self._started:
            await self.startup()

        gate_token = CURRENT_CALL.set(
            {
                "session_id": request.session_id,
                "request_id": request.request_id,
                "plan_id": "",
                "task_id": "",
            }
        )
        self.bus.emit(
            "brain.turn_started",
            phase=EventPhase.BRAIN,
            request_id=request.request_id,
            session_id=request.session_id,
            language=request.language,
            source=source,
            chars=len(cleaned),
        )

        context: Context
        reasoning: Reasoning
        plan: Plan
        reflection: Reflection
        try:
            # 1. memory + session context BEFORE any planning (blueprint rule)
            context = await self.context_builder.build(
                request, screen_summary=screen_summary, recent_tasks=self._last_plans.get(request.session_id, [])
            )
            # 2. reasoning (router → LLM provider → offline fallback), sanitised
            reasoning = await self.reasoner.reason(request, context)
            # 3. structured plan with task states + risk + confirmation flags
            plan = await self.planner.plan(request, context, reasoning)
            # 4. next-action prediction (suggestions; never executed here)
            plan.predicted_next = await self.predictor.predict(request, context, plan)
            # 5. execution: only through the orchestrator seam (Phase 10) — the
            #    legacy router already executed its own deterministic actions.
            if self.executor is not None and any(
                call.state.value == "proposed" for task in plan.tasks for call in task.steps
            ):
                plan = await self.executor.execute(plan, context=context)
            # 6. reflection: expected vs observed, retry / recover / escalate
            reflection = await self.reflector.reflect(request, context, plan, reasoning)
        except Exception as exc:  # noqa: BLE001 — a brain failure must still answer
            CURRENT_CALL.reset(gate_token)
            self.stats["failures"] += 1
            self.bus.emit(
                "task.failed",
                phase=EventPhase.BRAIN,
                request_id=request.request_id,
                session_id=request.session_id,
                error=repr(exc),
                stage="brain",
            )
            _log.exception("brain turn failed")
            return {
                "ok": False,
                "response": (
                    "দুঃখিত বন্ধু, ভেতরে একটা গোলমাল হয়েছে — আবার বলবে?"
                    if request.language != "en"
                    else "Sorry, something went wrong inside my brain. Could you say that again?"
                ),
                "error": repr(exc),
                "source": "brain:error",
                "language": request.language,
                "session_id": request.session_id,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "events_published": True,
            }

        # 7. learn from what actually ran, and remember the turn (episodic)
        tools_now = [call.tool for task in plan.tasks for call in task.steps]
        self.predictor.learn(plan)
        previous = self._last_tool.get(request.session_id)
        if previous is not None and tools_now and previous[1] and reflection.ok:
            self.predictor.learn_bridge(previous[0], tools_now[0])
        if tools_now:
            self._last_tool[request.session_id] = (tools_now[-1], reflection.ok)
        await self._remember_turn(request, reasoning, plan, reflection)

        CURRENT_CALL.reset(gate_token)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        self.stats["turns"] += 1
        self.stats["llm_turns"] += int(reasoning.used_llm)
        self.stats["plans"] += 1
        self.stats["tool_calls"] += sum(len(task.steps) for task in plan.tasks)
        self.stats["escalations"] += int(reflection.should_escalate)
        self.stats["last_latency_ms"] = latency_ms
        self._last_plans[request.session_id] = [
            {"goal": task.goal, "agent": task.agent.value, "state": task.state.value, "risk": task.risk}
            for task in plan.tasks
        ][-5:]

        response_text = reasoning.response_text or reflection.suggestion
        honest_note = ""
        simulated = any(
            str(call.result.get("decision") or "") == "simulated"
            for task in plan.tasks
            for call in task.steps
        )
        # the legacy persona often answers in Bengali even to an English request,
        # so the correction follows the *reply* language, not the request language
        from Backend.star.voice.language import detect_language as _detect

        reply_language = "en" if _detect(response_text).code == "en" else "bn"
        notes = HONEST_NOTES[reply_language]
        if reflection.verdict in notes and reasoning.response_text:
            honest_note = notes[reflection.verdict]
        elif simulated and reasoning.response_text:
            honest_note = DRY_RUN_NOTES[reply_language]
        if honest_note:
            response_text = f"{response_text} {honest_note}".strip()
        result: dict[str, Any] = {
            "ok": reflection.ok or not plan.tasks[1:],
            "response": response_text,
            "intent": reasoning.intent,
            "source": f"brain:{reasoning.source}",
            "language": request.language,
            "session_id": request.session_id,
            "latency_ms": latency_ms,
            "dry_run": self.settings.security.dry_run,
            "request_id": request.request_id,
            "plan": plan.public(),
            "tasks": [task.public() for task in plan.tasks],
            "task_count": len([task for task in plan.tasks if task.steps]),
            "reflection": reflection.model_dump(mode="json"),
            "predictions": [p.model_dump(mode="json") for p in plan.predicted_next],
            "reasoning": {
                "intent": reasoning.intent,
                "source": reasoning.source,
                "used_llm": reasoning.used_llm,
                "confidence": round(reasoning.confidence, 3),
                "latency_ms": reasoning.latency_ms,
                "actions": len(reasoning.actions),
                "proposals": len(reasoning.proposed_calls),
                "notes": reasoning.notes,
            },
            "context": context.compact(),
            "memory_hits": len(context.retrieved_memory),
            "honest_note": honest_note,
            "simulated": simulated,
            "events_published": True,
        }
        self.bus.emit(
            "brain.turn_completed",
            phase=EventPhase.BRAIN,
            request_id=request.request_id,
            session_id=request.session_id,
            ok=result["ok"],
            verdict=reflection.verdict,
            intent=reasoning.intent,
            source=reasoning.source,
            used_llm=reasoning.used_llm,
            tasks=len(plan.tasks),
            tool_calls=result["task_count"],
            predictions=len(plan.predicted_next),
            latency_ms=latency_ms,
        )
        return result

    # ── memory writes ────────────────────────────────────────────────────────
    async def _remember_turn(
        self, request: UserRequest, reasoning: Reasoning, plan: Plan, reflection: Reflection
    ) -> None:
        if self.store is None:
            return
        try:
            from agent.core.enums import MemoryKind

            tools = [call.tool for task in plan.tasks for call in task.steps]
            episode = f"{request.text[:180]} → {reasoning.intent}" + (f" [{', '.join(tools[:6])}]" if tools else "")
            await asyncio.to_thread(self.store.remember, episode, kind=MemoryKind.EPISODE)
            self.bus.emit(
                "memory.updated",
                phase=EventPhase.BRAIN,
                session_id=request.session_id,
                request_id=request.request_id,
                layer="episodic",
                memory_kind="turn",
                text=request.text[:120],
                tools=tools[:6],
                verdict=reflection.verdict,
            )
        except Exception as exc:  # noqa: BLE001 — memory writes must never break a turn
            _log.warning("episodic write failed: %s", exc)

    # ── introspection ────────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "name": "star-brain",
            "phase": 3,
            "started": self._started,
            "memory": self.retriever.name,
            "reasoners": self.reasoner.describe() if hasattr(self.reasoner, "describe") else [],
            "planner": self.planner.describe(),
            "predictor": self.predictor.describe() if hasattr(self.predictor, "describe") else {"name": self.predictor.name},
            "reflector": self.reflector.describe() if hasattr(self.reflector, "describe") else {"name": self.reflector.name},
            "context": self.context_builder.describe(),
            "executor": getattr(self.executor, "name", None),
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if not getattr(self.reasoner, "available", lambda: True)():
            problems.append("no reasoner available")
        if self.stats["failures"] > max(3, self.stats["turns"] // 2):
            problems.append("high brain failure rate")
        status = "ok" if not problems else "degraded"
        return {
            "status": status,
            "detail": {
                "memory": self.retriever.name,
                "reasoners": [getattr(r, "name", "?") for r in getattr(self.reasoner, "reasoners", [])],
                "reflection": self.settings.brain.reflection_enabled,
                "prediction": self.settings.brain.prediction_enabled,
                "turns": self.stats["turns"],
                "problems": problems,
            },
        }

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "session_id": session_id,
            "recent_plans": self._last_plans.get(session_id or self.settings.session_id, []),
            "working_memory": self.context_builder.working_memory(session_id or self.settings.session_id),
            **self.describe(),
        }


def build_brain(settings: Settings, *, bus: StarEventBus | None = None, **slots: Any) -> StarBrain:
    """Factory: real reasoners/planner/predictor/reflector over the existing memory store."""
    store = slots.pop("store", None)
    if store is None and slots.get("retriever") is None:
        store = open_memory_store(settings)
    return StarBrain(settings, bus=bus, store=store, **slots)

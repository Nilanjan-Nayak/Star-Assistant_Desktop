"""STAR 2.0 composition root.

``StarApplication`` is the object the gateway talks to (see
:mod:`Backend.star.contracts`). It owns the subsystem slots and grows phase by
phase; every slot is optional and every accessor degrades gracefully so the
gateway never 500s just because a later phase is not wired yet.

Run it::

    python -m Backend.star --port 8765            # gateway + ops console
    python -m Backend.star --print-info           # no socket, just the manifest
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from typing import Any

from Backend.star import PROTOCOL_VERSION, __codename__, __version__
from Backend.star.config.settings import Settings, get_settings
from Backend.star.gateway.events import AgentEventBridge
from Backend.star.gateway.server import GatewayServer
from Backend.star.observability.events import EventPhase, StarEventBus, get_event_bus
from Backend.star.observability.logging import attach_file_logging, star_logger
from Backend.star.observability.metrics import get_metrics
from Backend.star.brain.pipeline import StarBrain, build_brain, open_memory_store
from Backend.star.agents.base import AgentRegistry
from Backend.star.brain.planning import StarPlanner
from Backend.star.browser.agent import build_browser
from Backend.star.computer.agent import build_computer
from Backend.star.memory.manager import build_memory
from Backend.star.memory.tools import register_memory_tools
from Backend.star.learning.loop import build_learning
from Backend.star.learning.tools import register_learning_tools
from Backend.star.orchestrator import build_orchestrator, register_orchestrator_tools
from Backend.star.workspace.manager import WorkspaceError, build_workspace
from Backend.star.workspace.tools import register_workspace_tools
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.security.surface import SecuritySurface
from Backend.star.security.tools import register_security_tools
from Backend.star.tools.executor import ToolExecutor
from Backend.star.tools.permissions import PermissionEngine
from Backend.star.tools.registry import StarToolRegistry, build_tool_registry
from Backend.star.voice.language import detect_language, normalize_text, response_language
from Backend.star.voice.pipeline import VoicePipeline, build_voice_pipeline

__all__ = ["StarApplication", "build_application", "serve", "main"]

_log = star_logger("app")


def _phase_pending(phase: int, what: str) -> dict[str, Any]:
    return {
        "ok": True,
        "pending": True,
        "phase": phase,
        "detail": f"{what} is wired in Phase {phase} of the STAR 2.0 build plan",
    }


class StarApplication:
    """The Star 2.0 brain container (gateway-facing surface)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: StarEventBus | None = None,
        brain: Any = None,
        orchestrator: Any = None,
        executor: Any = None,
        tools: Any = None,
        memory: Any = None,
        security: Any = None,
        workspace: Any = None,
        voice: Any = None,
        learning: Any = None,
        agents: Any = None,
        browser: Any = None,
        computer: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.bus = bus or get_event_bus(history_size=self.settings.gateway.event_buffer)
        self.bridge = AgentEventBridge(self.bus, session_id=self.settings.session_id)
        self.brain = brain
        self.orchestrator = orchestrator
        self.executor = executor
        self.memory = memory
        self.security = security
        self.workspace = workspace
        self.voice = voice
        self.learning = learning
        self.browser = browser
        self.computer = computer
        # NB: stored as *_registry because ``agents()`` / ``tools()`` are the
        # gateway-facing accessor methods required by StarApplicationProtocol.
        self.agent_registry = agents
        self.tool_registry = tools
        self.created_at = time.time()
        self._tasks: list[dict[str, Any]] = []
        self._plans: list[dict[str, Any]] = []
        self._stopped = False
        self.metrics = get_metrics()

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def startup(self) -> None:
        self.settings.paths.ensure()
        attach_file_logging(self.settings.paths.logs_dir)
        self.bridge.attach()
        self.bus.bind_loop()
        for subsystem in (
            self.security,
            self.tool_registry,
            self.executor,
            self.agent_registry,
            self.browser,
            self.computer,
            self.orchestrator,
            self.memory,
            self.workspace,
            self.voice,
            self.learning,
            self.brain,
        ):
            starter = getattr(subsystem, "startup", None)
            if starter is not None:
                with contextlib.suppress(Exception):
                    result = starter()
                    if asyncio.iscoroutine(result):
                        await result
        self.bus.emit(
            "agent.ready",
            phase=EventPhase.SYSTEM,
            version=__version__,
            dry_run=self.settings.security.dry_run,
            session_id=self.settings.session_id,
        )
        _log.info("star2.application.ready")

    async def aclose(self) -> None:
        for subsystem in (
            self.brain,
            self.voice,
            self.workspace,
            self.learning,
            self.memory,
            self.orchestrator,
            self.computer,
            self.browser,
            self.agent_registry,
            self.executor,
            self.tool_registry,
            self.security,
        ):
            closer = getattr(subsystem, "aclose", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    result = closer()
                    if asyncio.iscoroutine(result):
                        await result
        self.bridge.detach()

    # ── introspection ───────────────────────────────────────────────────────
    def capabilities(self) -> list[str]:
        caps = ["gateway.http", "gateway.websocket", "events.stream", "console"]
        for name, slot in (
            ("voice", self.voice),
            ("brain", self.brain),
            ("orchestrator", self.orchestrator),
            ("executor", self.executor),
            ("tools", self.tool_registry),
            ("agents", self.agent_registry),
            ("browser", self.browser),
            ("computer", self.computer),
            ("memory", self.memory),
            ("learning", self.learning),
            ("security", self.security),
            ("workspace", self.workspace),
        ):
            if slot is not None:
                caps.append(name)
        return caps

    def info(self) -> dict[str, Any]:
        return {
            "ok": True,
            "name": __codename__,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "session_id": self.settings.session_id,
            "owner": self.settings.owner,
            "capabilities": self.capabilities(),
            "settings": self.settings.public_dict(),
            "uptime_s": round(time.time() - self.created_at, 2),
        }

    def health(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "gateway": {"status": "ok", "detail": "asyncio http+ws server"},
            "event_bus": {"status": "ok", "detail": self.bus.snapshot()},
            "brain": _slot_health(self.brain),
            "orchestrator": _slot_health(self.orchestrator),
            "executor": _slot_health(self.executor),
            "tools": _slot_health(self.tool_registry),
            "agents": _slot_health(self.agent_registry),
            "browser": _slot_health(self.browser),
            "computer": _slot_health(self.computer),
            "memory": _slot_health(self.memory),
            "security": _slot_health(self.security),
            "workspace": _slot_health(self.workspace),
            "voice": _slot_health(self.voice),
            "learning": _slot_health(self.learning),
        }
        statuses = [str(value.get("status", "pending")) for value in checks.values()]
        overall = "fail" if "fail" in statuses else "degraded" if "degraded" in statuses else "ok"
        return {"ok": overall == "ok", "status": overall, "checks": checks, "dry_run": self.settings.security.dry_run}

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "info": self.info(),
            "health": self.health(),
            "metrics": self.metrics.snapshot()["star"],
            "tasks_running": sum(1 for task in self._tasks if task.get("state") == "running"),
            "tasks_total": len(self._tasks),
            "stopped": self._stopped,
            "events": self.bus.snapshot(),
        }

    def agents(self) -> list[dict[str, Any]]:
        if self.agent_registry is not None:
            return list(self.agent_registry.describe())
        return []

    def tools(self) -> list[dict[str, Any]]:
        if self.tool_registry is not None:
            return list(self.tool_registry.describe())
        return []

    def tasks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        source = self.orchestrator.tasks() if self.orchestrator is not None else self._tasks
        return list(source)[-limit:]

    def plans(self, *, limit: int = 20) -> list[dict[str, Any]]:
        source = self.orchestrator.plans() if self.orchestrator is not None else self._plans
        return list(source)[-limit:]

    def workspaces(self) -> list[dict[str, Any]]:
        if self.workspace is not None:
            return list(self.workspace.describe())
        return []

    def confirmations(self) -> list[dict[str, Any]]:
        if self.security is not None:
            return list(self.security.pending())
        return []

    def memory_snapshot(self, query: str | None = None, *, limit: int = 20) -> dict[str, Any]:
        if self.memory is not None:
            return self.memory.snapshot(query, limit=limit)
        return {"ok": True, "query": query, **_phase_pending(8, "the layered memory manager")}

    async def memory_snapshot_async(self, query: str | None = None, *, limit: int = 20) -> dict[str, Any]:
        """Same payload without the thread hop — the gateway prefers this one."""
        probe = getattr(self.memory, "snapshot_async", None)
        if callable(probe):
            return await probe(query, limit=limit)
        return self.memory_snapshot(query, limit=limit)

    def learning_snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        """The safe learning loop: gates, candidates, feedback, promotions (``GET /api/v1/learning``)."""
        if self.learning is not None:
            return self.learning.snapshot(limit=limit)
        return {"ok": True, **_phase_pending(9, "the safe learning loop")}

    def learning_feedback(self, **kwargs: Any) -> dict[str, Any]:
        """Record explicit feedback (``POST /api/v1/learning``)."""
        if self.learning is not None:
            return self.learning.record_feedback(**kwargs)
        return {"ok": False, "error": "learning loop not wired", **_phase_pending(9, "learning feedback")}

    def learning_cycle(self, **kwargs: Any) -> dict[str, Any]:
        """Run one validation + promotion sweep on demand."""
        if self.learning is not None:
            return self.learning.cycle(**kwargs)
        return {"ok": False, "error": "learning loop not wired", **_phase_pending(9, "a learning cycle")}

    # ── Phase 8: what Star remembers about the interaction it just had ─────
    async def _remember_chat(self, text: str, result: dict[str, Any], *, session_id: str) -> None:
        """Keep the reply in working memory, and the exchange in episodic memory.

        A chat turn only becomes an episode when Star actually called a tool for it —
        otherwise episodic memory would fill up with small talk and stop being a useful
        record of *what Star did*. The learning loop observes the same turn (Phase 9):
        a finished multi-tool turn is the raw material for a *validated* pattern.
        """
        # Phase 9: observe first so learning still happens even if memory is unwired
        if self.learning is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.learning.observe_chat, result, session_id=session_id)
        if self.memory is None:
            return
        reply = str(result.get("response") or "").strip()
        if reply:
            self.memory.remember_turn(session_id, f"star: {reply[:200]}")
        tasks = [task for task in (result.get("tasks") or []) if isinstance(task, dict)]
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        steps: list[dict[str, Any]] = []
        for task in tasks or (plan or {}).get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for call in task.get("steps") or []:
                if isinstance(call, dict):
                    steps.append(
                        {
                            "tool": call.get("tool") or "step",
                            "arguments": call.get("arguments") or {},
                            "ok": str(call.get("state") or "").lower() == "done",
                            "note": str(call.get("error") or task.get("summary") or "")[:200],
                        }
                    )
        if not steps:
            return                      # small talk is not an episode: Star did nothing
        succeeded = bool(result.get("ok", True)) and not any(
            str(task.get("state") or "").lower() == "failed" for task in tasks
        )
        with contextlib.suppress(Exception):
            await self.memory.record_episode(
                text, steps=steps, succeeded=succeeded, session_id=session_id, agent="conversation"
            )

    async def _remember_run(self, agent: str, goal: str, run: Any, *, session_id: str) -> None:
        """A finished agent run is one episode: the goal, the tools it took, the outcome."""
        # Phase 9: the learning loop observes the run's tool sequence (dry-runs record
        # the sighting but withhold the outcome, so a rehearsal never proves a pattern).
        if self.learning is not None and run is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.learning.observe_run, run, session_id=session_id, agent=agent)
        if self.memory is None or run is None:
            return
        steps = [
            {
                "tool": step.tool or step.action or "step",
                "arguments": dict(step.arguments or {}),
                "ok": bool(step.ok),
                "expect": step.expected,
                "note": (step.error or step.decision or "")[:200],
            }
            for step in getattr(run, "steps", [])
            if not getattr(step, "skipped", False)
        ]
        # dry-run episodes stay truthful: they record the *intention*, labelled as such
        text = f"[dry-run] {goal}" if getattr(run, "dry_run", False) else goal
        with contextlib.suppress(Exception):
            await self.memory.record_episode(
                text,
                steps=steps,
                succeeded=bool(getattr(run, "succeeded", False)),
                session_id=session_id or getattr(run, "session_id", ""),
                agent=agent,
            )

    def audit_tail(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if self.security is not None:
            return list(self.security.audit_tail(limit=limit))
        return []

    def event_history(
        self, *, limit: int = 50, kinds: frozenset[str] | None = None, since_seq: int = 0
    ) -> list[dict[str, Any]]:
        return [event.public() for event in self.bus.history(limit=limit, kinds=kinds, since_seq=since_seq)]

    # ── actions ─────────────────────────────────────────────────────────────
    async def chat(
        self,
        text: str,
        *,
        session_id: str | None = None,
        language: str | None = None,
        source: str = "text",
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        sid = session_id or self.settings.session_id

        # ── Phase 2: language routing (Bengali / Banglish / English / mixed) ──
        cleaned = normalize_text(text) or text
        profile = detect_language(cleaned)   # detect AFTER wake-word stripping
        answer_language = response_language(profile, default=self.settings.voice.default_response_language)
        self.bus.emit(
            "request.received",
            phase=EventPhase.GATEWAY,
            session_id=sid,
            text=cleaned[:400] if self.settings.voice.keep_transcripts else f"<{len(cleaned)} chars>",
            source=source,
            language=language or profile.code,
            banglish=profile.banglish,
            confidence=round(profile.confidence, 3),
        )
        self.bus.emit("language.detected", phase=EventPhase.VOICE, session_id=sid, **profile.as_dict())
        if self.voice is not None and isinstance(self.voice, VoicePipeline):
            self.voice.last_transcript = None  # text input: no transcript retention needed

        if self._stopped:
            return {
                "ok": False,
                "response": "Emergency stop is active. Clear it to continue.",
                "error": "emergency_stop_active",
                "session_id": sid,
            }

        resolved_language = language or answer_language
        if self.brain is not None:
            result = await self.brain.handle(
                cleaned, session_id=sid, language=resolved_language, source=source
            )
        else:
            # Phase 1/2 fallback: reuse the existing deterministic router so the
            # gateway is useful before the brain lands (Phase 3).
            result = await self._legacy_route(
                cleaned, session_id=sid, language=resolved_language, source=source
            )
        result.setdefault("language_profile", profile.as_dict())
        result.setdefault("language", resolved_language)
        await self._maybe_speak(result, language=resolved_language)
        await self._remember_chat(cleaned, result, session_id=sid)
        self._record(result)
        return result

    async def _maybe_speak(self, result: dict[str, Any], *, language: str) -> None:
        """Synthesise the reply only when auto-speak is on (the HUD owns playback)."""
        if not self.settings.voice.auto_speak or self.voice is None:
            return
        text = str(result.get("response") or "").strip()
        if not text:
            return
        try:
            synthesis = await self.voice.speak(text, language=language)
        except Exception as exc:  # noqa: BLE001 — speech must never break a reply
            self.bus.emit("task.failed", phase=EventPhase.VOICE, error=repr(exc), stage="tts")
            return
        if synthesis.ok:
            result["audio_path"] = synthesis.audio_path
            result["audio_voice"] = synthesis.voice
            result["audio_provider"] = synthesis.provider

    async def _legacy_route(
        self, text: str, *, session_id: str, language: str | None, source: str
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            from Backend.nlu.command_router import route_command

            routed = await asyncio.to_thread(route_command, text)
        except Exception as exc:  # noqa: BLE001 — legacy router is best-effort
            routed = None
            _log.warning("legacy.router.unavailable error=%s", exc)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if routed and routed.get("response"):
            actions = routed.get("actions") or []
            self.bus.emit(
                "task.completed",
                phase=EventPhase.TASK,
                session_id=session_id,
                source="legacy_router",
                actions=[{k: v for k, v in action.items() if k != "result"} for action in actions],
            )
            return {
                "ok": True,
                "response": routed["response"],
                "source": f"legacy_router:{routed.get('source', 'command_router')}",
                "language": language or _detect_language(text),
                "session_id": session_id,
                "latency_ms": latency_ms,
                "task_count": len(actions),
                "dry_run": self.settings.security.dry_run,
                "note": "Phase 1 path — the STAR 2.0 brain (Phase 3) replaces this.",
            }
        return {
            "ok": True,
            "response": (
                "শুনেছি তোমার কথা। এখন আমি গেটওয়ে মোডে আছি — পুরো ব্রেন Phase 3 এ যুক্ত হচ্ছে, "
                "ততক্ষণ এই কনসোলে সব ইভেন্ট দেখতে পাবে।"
                if (language or _detect_language(text)) == "bn"
                else "Heard you. I am running in gateway mode — the full brain lands in Phase 3; "
                "until then every event is visible in this console."
            ),
            "source": "gateway_echo",
            "language": language or _detect_language(text),
            "session_id": session_id,
            "latency_ms": latency_ms,
            "task_count": 0,
            "dry_run": self.settings.security.dry_run,
        }

    def _record(self, result: dict[str, Any]) -> None:
        """Persist + publish whatever a pipeline returned, so every layer sees it."""
        session_id = str(result.get("session_id") or self.settings.session_id)
        plan = result.get("plan")
        published = bool(result.get("events_published"))
        if isinstance(plan, dict):
            self._plans.append(plan)
            self._plans = self._plans[-50:]
            if self.orchestrator is not None:
                self.orchestrator.record_plan(plan)
            tasks = [task for task in (plan.get("tasks") or []) if isinstance(task, dict)]
            if not published:
                self.bus.emit(
                    "plan.created",
                    phase=EventPhase.PLAN,
                    session_id=session_id,
                    plan_id=plan.get("plan_id"),
                    intent=plan.get("intent"),
                    rationale=plan.get("rationale"),
                    tasks=len(tasks),
                    requires_confirmation=bool(plan.get("requires_confirmation")),
                )
            for task in tasks:
                self._tasks.append(task)
                self._publish_task_state(task, session_id=session_id)
        for task in result.get("tasks") or []:
            if isinstance(task, dict):
                self._tasks.append(task)
                self._publish_task_state(task, session_id=session_id)
                if self.orchestrator is not None:
                    self.orchestrator.record_task(task)
        self._tasks = self._tasks[-200:]
        audio = result.get("audio_url") or result.get("audio_path")
        if audio:
            self.bus.emit(
                "response.spoken",
                phase=EventPhase.VOICE,
                session_id=session_id,
                text=str(result.get("response") or "")[:200],
                audio=str(audio),
            )

    def _publish_task_state(self, task: dict[str, Any], *, session_id: str) -> None:
        state = str(task.get("state") or "").lower()
        common = {
            "session_id": session_id,
            "task_id": task.get("task_id"),
            "goal": task.get("goal"),
            "agent": task.get("agent"),
            "state": state,
            "risk": task.get("risk"),
            "summary": task.get("summary"),
        }
        if state in {"done", "succeeded", "completed", "verified"}:
            self.bus.emit("task.completed", phase=EventPhase.TASK, **common)
        elif state in {"failed", "error"}:
            self.bus.emit("task.failed", phase=EventPhase.TASK, error=task.get("error"), **common)
        elif state in {"cancelled", "aborted"}:
            self.bus.emit("task.cancelled", phase=EventPhase.TASK, **common)
        elif state in {"recovered"}:
            self.bus.emit("task.recovered", phase=EventPhase.TASK, **common)
        elif state in {"running", "pending", "verifying", "waiting_confirmation"}:
            self.bus.emit("task.progress", phase=EventPhase.TASK, **common)

    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = str(message.get("type") or "").lower()
        if kind.startswith("ui."):
            # The existing HUD mirroring its reactor state / log lines. Published
            # on the mission bus so the console + audit trail can follow the UI.
            payload = {k: v for k, v in message.items() if k != "type"}
            self.bus.emit(
                "task.progress",
                phase=EventPhase.GATEWAY,
                session_id=str(message.get("session_id") or self.settings.session_id),
                ui=kind,
                **payload,
            )
            return {"ok": True, "mirrored": kind}
        if self.voice is not None and kind.startswith("voice."):
            return await self.voice.handle_message(message)
        if self.workspace is not None and kind.startswith("workspace."):
            return await self.workspace.handle_message(message)
        if self.learning is not None and kind.startswith(("feedback", "learning.")):
            return await self.learning.handle_message(message)
        return {"ok": False, "error": f"unknown message type: {kind or '<empty>'}"}

    def browser_state(self) -> dict[str, Any]:
        """Browser agent + guardrails + the last run (``GET /api/v1/browser``)."""
        if self.browser is None:
            return {"ok": False, "enabled": False, **_phase_pending(5, "browser agent")}
        agent = self.browser
        last = agent.last_run()
        return {
            "ok": True,
            "enabled": self.settings.browser.enabled,
            "agent": agent.describe(),
            "settings": {
                "allowed_schemes": list(self.settings.browser.allowed_schemes),
                "allow_private_hosts": self.settings.browser.allow_private_hosts,
                "auto_open": self.settings.browser.auto_open,
                "max_steps": self.settings.browser.max_steps,
                "timeout_s": self.settings.browser.timeout_s,
                "max_bytes": self.settings.browser.max_bytes,
            },
            "last_run": last.public() if last is not None else None,
            "runs": len(agent.runs),
        }

    async def run_browser_goal(self, goal: str, *, session_id: str = "", dry_run: bool | None = None) -> dict[str, Any]:
        """Run one browser goal end-to-end (``POST /api/v1/browser``)."""
        if self.browser is None:
            return {"ok": False, "error": "browser agent not wired", **_phase_pending(5, "browser agent")}
        if not (goal or "").strip():
            return {"ok": False, "error": "field 'goal' is required"}
        if self._stopped:
            return {"ok": False, "error": "emergency stop is active", "stopped": True}
        run = await self.browser.run(
            goal,
            session_id=session_id or self.settings.session_id,
            dry_run=dry_run,
        )
        await self._remember_run("browser", goal, run, session_id=session_id or self.settings.session_id)
        payload = run.public()
        payload["ok"] = run.succeeded
        if run.dry_run and run.succeeded:
            payload["note"] = "dry-run: nothing was really opened, fetched or clicked"
        return payload

    def computer_state(self) -> dict[str, Any]:
        """Computer agent + motor + guardrails + the last run (``GET /api/v1/computer``)."""
        if self.computer is None:
            return {"ok": False, "enabled": False, **_phase_pending(6, "computer agent")}
        agent = self.computer
        last = agent.last_run()
        toolkit = getattr(agent, "toolkit", None)
        return {
            "ok": True,
            "enabled": self.settings.computer.enabled,
            "motor": toolkit.motor.describe() if toolkit is not None else None,
            "guardrails": toolkit.guard.describe() if toolkit is not None else None,
            "agent": agent.describe(),
            "settings": {
                "backend": self.settings.computer.backend,
                "screen": f"{self.settings.computer.screen_width}x{self.settings.computer.screen_height}",
                "max_steps": self.settings.computer.max_steps,
                "blocked_hotkeys": list(self.settings.computer.blocked_hotkeys),
                "forbidden_regions": list(self.settings.computer.forbidden_regions),
            },
            "dry_run": self.settings.security.dry_run,
            "last_run": last.public() if last is not None else None,
            "runs": len(agent.runs),
        }

    async def run_computer_goal(self, goal: str, *, session_id: str = "", dry_run: bool | None = None) -> dict[str, Any]:
        """Run one computer-control goal (``POST /api/v1/computer``)."""
        if self.computer is None:
            return {"ok": False, "error": "computer agent not wired", **_phase_pending(6, "computer agent")}
        if not (goal or "").strip():
            return {"ok": False, "error": "field 'goal' is required"}
        if self._stopped:
            return {"ok": False, "error": "emergency stop is active", "stopped": True}
        run = await self.computer.run(goal, session_id=session_id or self.settings.session_id, dry_run=dry_run)
        await self._remember_run("computer", goal, run, session_id=session_id or self.settings.session_id)
        payload = run.public()
        payload["ok"] = run.succeeded
        if run.dry_run and run.succeeded:
            payload["note"] = "dry-run: the mouse and keyboard did not move — the intended actions are recorded"
        return payload

    # ── Phase 7: the invisible background workspace ────────────────────────
    def workspace_state(self) -> dict[str, Any]:
        """Manager state + every session (``GET /api/v1/workspaces``)."""
        if self.workspace is None:
            return {"ok": False, "enabled": False, "workspaces": [], **_phase_pending(7, "the background workspace")}
        return self.workspace.state()

    def workspace_session(self, session_id: str) -> dict[str, Any]:
        """One session in detail (``GET /api/v1/workspaces/{id}``)."""
        if self.workspace is None:
            return {"ok": False, "error": "the background workspace is not wired"}
        session = self.workspace.get(session_id)
        if session is None:
            return {"ok": False, "error": f"unknown workspace session '{session_id}'"}
        data = self.workspace._public(session)
        data["ok"] = True
        data["files_list"] = self.workspace.list_files(session)
        data["checkpoints"] = [item.public() for item in session.checkpoints]
        return data

    async def _workspace_tool(self, tool: str, arguments: dict[str, Any], *, dry_run: bool | None = None) -> dict[str, Any]:
        """Run a mutating workspace call through the executor so the whole Phase 4
        ladder applies: risk → permissions → confirmation → audit → dry-run."""
        if self.workspace is None:
            return {"ok": False, "error": "the background workspace is not wired"}
        if self._stopped:
            return {"ok": False, "error": "emergency stop is active", "stopped": True}
        if self.executor is None:
            return {"ok": False, "error": "no tool executor is wired"}
        result = await self.executor.call(tool, arguments, session_id=self.settings.session_id, dry_run=dry_run)
        payload = result.model_dump(mode="json")
        payload["ok"] = bool(result.ok)
        payload.setdefault("decision", result.decision)
        if result.confirmation_id:
            payload["note"] = f"{tool} needs your approval — resolve {result.confirmation_id}"
        return payload

    async def create_workspace(
        self, kind: str = "generic", label: str = "", ttl_s: float | None = None, *, dry_run: bool | None = None
    ) -> dict[str, Any]:
        return await self._workspace_tool(
            "workspace_create", {"kind": kind, "label": label, **({"ttl_s": ttl_s} if ttl_s else {})}, dry_run=dry_run
        )

    async def close_workspace(self, session_id: str, *, destroy: bool = False, dry_run: bool | None = None) -> dict[str, Any]:
        # the tool argument is `remove_files`, and it is only sent when asked for:
        # its mere presence raises the call to high risk (see workspace/tools.py)
        arguments: dict[str, Any] = {"session_id": session_id}
        if destroy:
            arguments["remove_files"] = True
        return await self._workspace_tool("workspace_close", arguments, dry_run=dry_run)

    async def checkpoint_workspace(self, session_id: str = "", label: str = "", *, dry_run: bool | None = None) -> dict[str, Any]:
        return await self._workspace_tool(
            "workspace_checkpoint", {"session_id": session_id, "label": label}, dry_run=dry_run
        )

    async def restore_workspace(self, session_id: str = "", checkpoint_id: str = "", *, dry_run: bool | None = None) -> dict[str, Any]:
        return await self._workspace_tool(
            "workspace_restore", {"session_id": session_id, "checkpoint_id": checkpoint_id}, dry_run=dry_run
        )

    async def resolve_confirmation(self, confirmation_id: str, *, approve: bool, note: str = "") -> dict[str, Any]:
        if self.security is None:
            return {"ok": False, "error": "security layer not wired yet", **_phase_pending(11, "confirmations")}
        return await self.security.resolve(confirmation_id, approve=approve, note=note)

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        if self.orchestrator is not None:
            return await self.orchestrator.cancel(task_id)
        return {"ok": False, "error": f"unknown task {task_id}", **_phase_pending(10, "task cancellation")}

    async def emergency_stop(self, *, reason: str = "user") -> dict[str, Any]:
        self._stopped = True
        self.metrics.inc("emergency_stop", reason=reason)
        cancelled: list[str] = []
        if self.orchestrator is not None:
            cancelled = await self.orchestrator.cancel_all(reason=f"emergency_stop:{reason}")
        if self.workspace is not None:
            with contextlib.suppress(Exception):
                await self.workspace.destroy_all(reason=f"emergency_stop:{reason}")
        if self.security is not None:
            with contextlib.suppress(Exception):
                self.security.mute(reason=f"emergency_stop:{reason}")
        self.bus.emit("emergency.stop", phase=EventPhase.SECURITY, reason=reason, cancelled=cancelled)
        _log.warning("star2.emergency_stop reason=%s cancelled=%s", reason, cancelled)
        return {"ok": True, "stopped": True, "reason": reason, "cancelled_tasks": cancelled}

    def resume(self) -> dict[str, Any]:
        self._stopped = False
        if self.security is not None:
            with contextlib.suppress(Exception):
                self.security.unmute()
        self.bus.emit("agent.ready", phase=EventPhase.SECURITY, resumed=True)
        return {"ok": True, "stopped": False}

    def security_status(self) -> dict[str, Any]:
        if self.security is not None:
            return self.security.describe()
        return {"ok": True, "phase": 11, "stopped": self._stopped}

    def security_audit(
        self,
        *,
        tool: str | None = None,
        session_id: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if self.security is not None:
            return self.security.audit_query(
                tool=tool, session_id=session_id, decision=decision, risk=risk, limit=limit
            )
        return []

    def security_scan(self, text: str = "") -> dict[str, Any]:
        if self.security is not None:
            has_secrets, kinds = self.security.scan_secrets(text)
            return {
                "ok": True,
                "has_secrets": has_secrets,
                "secret_kinds": kinds,
                "redacted_text": self.security.redact_secrets(text),
            }
        return {"ok": True, "has_secrets": False, "secret_kinds": [], "redacted_text": text}

    async def remember(
        self, text: str, *, kind: str = "fact", key: str | None = None, value: str | None = None
    ) -> dict[str, Any]:
        if self.memory is not None:
            return await self.memory.remember(text, kind=kind, key=key, value=value)
        return {"ok": False, "error": "memory layer not wired yet", **_phase_pending(8, "memory writes")}


def _slot_health(slot: Any) -> dict[str, Any]:
    if slot is None:
        return {"status": "pending", "detail": "not wired in this phase yet"}
    probe = getattr(slot, "health", None)
    if probe is None:
        return {"status": "ok", "detail": type(slot).__name__}
    try:
        result = probe()
        if isinstance(result, dict):
            return {"status": str(result.get("status", "ok")), "detail": result}
        return {"status": "ok", "detail": str(result)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "detail": repr(exc)}


def _detect_language(text: str) -> str:
    """Thin delegate to the Phase 2 language router."""
    return detect_language(text).code


def build_application(settings: Settings | None = None, **slots: Any) -> StarApplication:
    """Factory used by tests, scripts and :func:`serve`.

    Slots that are not supplied are built from settings, so the app is complete
    from Phase 2 onwards while staying injectable for tests.
    """
    cfg = settings or get_settings()
    bus = slots.get("bus")
    if bus is None:
        bus = get_event_bus(history_size=cfg.gateway.event_buffer)
    slots["bus"] = bus

    # ── Phase 4: tools → permissions → executor → security surface ──────────
    registry = slots.get("tools")
    if registry is None:
        registry = build_tool_registry(cfg, bus=bus)
        slots["tools"] = registry
    audit = AuditLog(cfg.security.audit_path, bus=bus)
    confirmations = ConfirmationStore(cfg, bus=bus)
    permissions = PermissionEngine(
        cfg, bus=bus, confirmations=confirmations, stop_gate=lambda: security.stopped
    )
    executor = ToolExecutor(
        cfg, registry=registry, permissions=permissions, audit=audit, confirmations=confirmations, bus=bus
    )
    if slots.get("security") is None:
        slots["security"] = SecuritySurface(
            cfg, bus=bus, audit=audit, confirmations=confirmations, permissions=permissions, executor=executor
        )
    security = slots["security"]
    if security is not None:
        register_security_tools(registry, cfg, bus=bus, security=security)
    slots.setdefault("executor", executor)         # Phase 10 adds the orchestrator above this

    # ── Phase 7: the invisible background workspace ────────────────────────
    # Blueprint §8 puts it in the flow itself: Permission → **Workspace** → Observe.
    if cfg.workspace.enabled and slots.get("workspace") is None:
        slots["workspace"] = build_workspace(cfg, bus=bus, audit=audit)
        register_workspace_tools(registry, cfg, bus=bus, manager=slots["workspace"])
    workspace = slots.get("workspace")

    # ── Phase 8: layered memory over the stores Star already has ───────────
    # Blueprint §9 — "inspect existing implementation before introducing another
    # memory store". So there is no new database here: the manager adapts
    # agent.memory.store.MemoryStore (SQLite), agent.planning.memory.EpisodicMemory
    # (JSONL), the brain's PatternStore (JSONL) and the brain's own per-session
    # scratch buffer. One connection, opened once, shared with the brain below.
    if slots.get("memory") is None:
        shared_store = open_memory_store(cfg) if cfg.memory.enabled else None
        slots["memory"] = build_memory(
            cfg, bus=bus, store=shared_store, own_store=shared_store is not None
        )
    memory = slots.get("memory")
    if memory is not None:
        register_memory_tools(registry, cfg, bus=bus, manager=memory)

    # ── Phase 5: the browser agent (first goal-oriented worker) ────────────
    if cfg.browser.enabled and slots.get("browser") is None:
        slots["browser"] = build_browser(
            cfg, bus=bus, executor=executor, registry=registry, stop_gate=lambda: security.stopped,
            workspace=workspace,
        )
    # ── Phase 6: the computer agent (screen / mouse / keyboard) ────────────
    if cfg.computer.enabled and slots.get("computer") is None:
        slots["computer"] = build_computer(
            cfg, bus=bus, executor=executor, registry=registry, stop_gate=lambda: security.stopped,
            workspace=workspace,
        )
    if slots.get("agents") is None:
        workers = [slot for slot in (slots.get("browser"), slots.get("computer")) if slot is not None]
        if workers:
            slots["agents"] = AgentRegistry(workers)

    # ── Phase 10: advanced orchestration (multi-agent, checkpoints, recovery) ──
    if slots.get("orchestrator") is None:
        agent_reg = slots.get("agents")
        slots["orchestrator"] = build_orchestrator(
            cfg,
            bus=bus,
            executor=executor,
            registry=agent_reg,
            workspace=workspace,
            stop_gate=lambda: security.stopped,
        )
    orchestrator = slots.get("orchestrator")
    if orchestrator is not None:
        register_orchestrator_tools(registry, cfg, bus=bus, orchestrator=orchestrator)

    if slots.get("voice") is None:
        slots["voice"] = build_voice_pipeline(cfg, bus=bus)
    if slots.get("brain") is None:
        risk_of = getattr(registry, "risk_of", None)
        planner = StarPlanner(cfg, bus=bus, **({"risk_classifier": risk_of} if callable(risk_of) else {}))
        brain_slots: dict[str, Any] = {}
        if memory is not None:
            brain_slots["retriever"] = memory              # memory is retrieved *before* planning
            if getattr(memory, "store", None) is not None:
                brain_slots["store"] = memory.store        # …over the very same SQLite connection
        slots["brain"] = build_brain(cfg, bus=bus, planner=planner, executor=orchestrator or executor, **brain_slots)

    # The brain already keeps per-session scratch and a live PatternStore: the working
    # and procedural layers borrow them instead of keeping a second copy of either.
    brain = slots.get("brain")
    if memory is not None and brain is not None:
        memory.attach_working(getattr(brain, "context_builder", None))
        memory.attach_patterns(getattr(getattr(brain, "predictor", None), "store", None))

    # ── Phase 9: the safe learning loop ──────────────────────────────────────
    # Blueprint §9 — interaction → observation → feedback → candidate → validation
    # → memory update → future retrieval, with "no uncontrolled self-modification of
    # executable code or model weights". The loop owns no long-term store: it keeps a
    # candidate ledger + feedback log (staging) and promotes only gate-cleared
    # candidates into the brain's PatternStore (procedural) and the shared
    # MemoryManager (preferences) — both borrowed, never reopened.
    # Always build the slot (like memory): a *disabled* loop still reports an honest
    # "off" health and an informative snapshot, and its four tools still answer, so the
    # surface is complete in every build. enabled=False means it touches no disk.
    if slots.get("learning") is None:
        risk_of = getattr(registry, "risk_of", None)
        slots["learning"] = build_learning(
            cfg, bus=bus,
            **({"risk_of": risk_of} if callable(risk_of) else {}),
        )
    learning = slots.get("learning")
    if learning is not None:
        # borrow the very stores memory/brain already built — one PatternStore, one MemoryStore
        learning.attach_patterns(getattr(getattr(brain, "predictor", None), "store", None))
        learning.attach_memory(memory)
        register_learning_tools(registry, cfg, bus=bus, loop=learning)
    return StarApplication(cfg, **slots)


async def serve(settings: Settings | None = None, *, host: str | None = None, port: int | None = None) -> GatewayServer:
    """Build the app + gateway, start it and serve until cancelled."""
    cfg = settings or get_settings()
    app = build_application(cfg)
    await app.startup()
    server = GatewayServer(app, cfg, host=host, port=port)
    await server.start()
    _log.info("star2.gateway.listening url=%s", server.url)
    print(f"[STAR 2.0] gateway listening on {server.url}  (console: {server.url}/)")
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await server.stop()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="star2", description="STAR 2.0 gateway / brain")
    parser.add_argument("--host", default=None, help="bind host (default from STAR_GATEWAY_HOST)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default from STAR_GATEWAY_PORT)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="force dry-run")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="allow real OS actions")
    parser.add_argument("--print-info", action="store_true", help="print the manifest and exit (no socket)")
    parser.add_argument("--print-health", action="store_true", help="print health and exit (no socket)")
    args = parser.parse_args(argv)

    import json as _json
    import os

    if args.dry_run is not None:
        os.environ["STAR_DRY_RUN"] = "true" if args.dry_run else "false"
    settings = get_settings(reload=True)

    if args.print_info or args.print_health:
        app = build_application(settings)

        async def _once() -> dict[str, Any]:
            await app.startup()
            payload = app.info() if args.print_info else app.health()
            await app.aclose()
            return payload

        print(_json.dumps(asyncio.run(_once()), ensure_ascii=False, indent=2, default=str))
        return 0

    try:
        asyncio.run(serve(settings, host=args.host, port=args.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

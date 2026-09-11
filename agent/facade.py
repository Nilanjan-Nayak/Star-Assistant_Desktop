"""Top-level ``ComputerControlAgent`` facade."""

from __future__ import annotations

import logging
from pathlib import Path
from types import TracebackType
from typing import Self

from agent.core.enums import HealthStatus, MemoryKind
from agent.core.health import HEALTH, HealthReport
from agent.core.ids import SkillName, parse_skill_name
from agent.core.logging import bind_correlation, get_logger, log_ctx, reset_correlation
from agent.core.metrics import METRICS
from agent.geometry.monitor import CURRENT_OS
from agent.memory.store import MemoryStore
from agent.memory.sync import FolderSync, SyncBackend
from agent.memory.types import MemoryRecord
from agent.motor.backends.null_backend import NullBackend
from agent.motor.backends.pyautogui_backend import PyAutoGUIBackend
from agent.motor.controller import MotorController
from agent.perception.cascade import PerceptionCascade
from agent.perception.ocr_engine import detect_engine, engine_name
from agent.world.capture import capture_available
from agent.planning.episode import Episode
from agent.planning.memory import EpisodicMemory
from agent.planning.planner import Planner
from agent.planning.planners.stub import StubPlanner
from agent.planning.react_agent import ReActAgent
from agent.safety.config import GovernorConfig
from agent.safety.governor import SafetyGovernor
from agent.skills.context import DefaultSkillContext
from agent.skills.registry import SkillRegistry
from agent.skills.result import SkillResult
from agent.world.capture import ScreenCapture
from agent.world.differ import ScreenDiffer
from agent.world.model import WorldModel

_log = get_logger("agent.facade")


class ComputerControlAgent:
    def __init__(
        self,
        governor_config: GovernorConfig | None = None,
        planner: Planner | None = None,
        dry_run: bool = False,
        world: WorldModel | None = None,
        memory_path: Path | str = Path("star_memory.db"),
        owner: str = "user",
        sync: SyncBackend | None = None,
    ) -> None:
        cfg = governor_config or GovernorConfig()
        cfg.dry_run = dry_run

        if world is None:
            capture = ScreenCapture()
            differ = ScreenDiffer()
            self.world = WorldModel(capture, differ)
        else:
            self.world = world

        try:
            backend = NullBackend() if dry_run else PyAutoGUIBackend()
        except Exception:
            backend = NullBackend()

        self.motor = MotorController(backend, self.world)
        self.perception = PerceptionCascade()
        self.governor = SafetyGovernor(cfg)
        self.context = DefaultSkillContext(
            motor=self.motor,
            perception=self.perception,
            world=self.world,
            governor=self.governor,
        )
        self.registry = SkillRegistry(self.context)
        self.registry.discover()
        self.memory = EpisodicMemory()
        self.semantic = MemoryStore(memory_path, owner=owner)
        self.sync = sync
        chosen_planner = planner or StubPlanner(semantic=self.semantic)
        self.agent = ReActAgent(
            registry=self.registry,
            motor=self.motor,
            world=self.world,
            governor=self.governor,
            planner=chosen_planner,
            memory=self.memory,
            semantic=self.semantic,
        )
        self._dry_run = dry_run

        HEALTH.register("clock", lambda: HealthStatus.OK)
        HEALTH.register(
            "backend",
            lambda: HealthStatus.OK if dry_run or not isinstance(backend, NullBackend) else HealthStatus.DEGRADED,
        )
        HEALTH.register(
            "memory",
            lambda: HealthStatus.OK if self.semantic.count() >= 0 else HealthStatus.FAIL,
        )
        HEALTH.register(
            "capture",
            lambda: HealthStatus.OK if capture_available() else HealthStatus.DEGRADED,
        )
        HEALTH.register(
            "ocr",
            lambda: HealthStatus.DEGRADED
            if engine_name(detect_engine()) == "empty"
            else HealthStatus.OK,
        )

        log_ctx(
            _log,
            logging.INFO,
            "agent.ready",
            os=CURRENT_OS.value,
            monitors=len(self.world.capture.monitors),
            skills=[str(s) for s in self.registry.discover()],
            dry_run=dry_run,
            safety_level=cfg.level.value,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, tb)
        self.governor.dump_audit()
        if self.sync is not None:
            try:
                self.sync.push(self.semantic.path)
            except Exception as exc:
                log_ctx(_log, logging.WARNING, "memory.sync.failed", error=str(exc))
        self.semantic.close()

    async def run(self, goal: str) -> Episode:
        token = bind_correlation()
        try:
            return await self.agent.run(goal)
        finally:
            reset_correlation(token)

    async def quick(self, skill: str, **params: object) -> SkillResult:
        name: SkillName = parse_skill_name(skill)
        return await self.registry.run(name, dict(params))

    def snapshot_metrics(self) -> dict[str, object]:
        return {
            "metrics": METRICS.snapshot(),
            "skill_stats": self.registry.stats(),
            "state": self.agent.fsm.state.value,
            "health": self.health().as_dict(),
            "dry_run": self._dry_run,
        }

    def health(self) -> HealthReport:
        return HEALTH.snapshot()

    def remember(
        self,
        text: str,
        *,
        kind: MemoryKind = MemoryKind.FACT,
        key: str | None = None,
        value: str | None = None,
    ) -> MemoryRecord:
        return self.semantic.remember(text, kind=kind, key=key, value=value)

    def recall(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        return self.semantic.recall_relevant(query, top_k=top_k)

    def memory_context(self, query: str) -> str:
        return self.semantic.build_context_block(query)

    def backup_memory(self, folder: Path | str) -> Path:
        return FolderSync(folder).push(self.semantic.path)

    async def see(
        self,
        query: str = "*",
        save_path: str | None = "see.png",
    ) -> SkillResult:
        """Capture the current screen and OCR it. Read-only."""
        params: dict[str, object] = {"query": query, "save_path": save_path}
        return await self.quick("see", **params)

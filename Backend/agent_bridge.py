"""
Agent Bridge for Star Assistant.
Provides a thread-safe, high-performance bridge between Star Assistant's
backend/UI threads and the ComputerControlAgent v6 (L0-L7 autonomous agent).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.core.enums import MemoryKind, SafetyLevel
from agent.facade import ComputerControlAgent
from agent.safety.config import GovernorConfig

_log = logging.getLogger("star.agent_bridge")

_INSTANCE: Optional[AgentBridge] = None
_LOCK = threading.Lock()


class AgentBridge:
    """Thread-safe manager for ComputerControlAgent.
    
    Runs a persistent background asyncio event loop on a dedicated daemon thread,
    allowing synchronous Qt workers and backend services to invoke asynchronous
    skills, OCR screen analysis, memory queries, and autonomous ReAct planning.
    """

    def __init__(
        self,
        memory_path: str = "star_memory.db",
        dry_run: bool = False,
        safety_level: SafetyLevel = SafetyLevel.NORMAL,
        owner: str = "Nilanjan",
    ) -> None:
        self.memory_path = Path(memory_path)
        self.dry_run = dry_run
        self.safety_level = safety_level
        self.owner = owner
        self._log_listeners: List[Callable[[str, str], None]] = []

        # Start dedicated background asyncio loop in a daemon thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop_worker,
            daemon=True,
            name="agent-async-worker"
        )
        self._thread.start()

        # Instantiate ComputerControlAgent inside the dedicated loop
        future = asyncio.run_coroutine_threadsafe(self._init_agent(), self._loop)
        self._agent: ComputerControlAgent = future.result(timeout=15.0)
        self._emit_log("[Agent] Computer Control Agent v6 চালু হয়েছে (OS: Windows, Memory: SQLite)", "ok")

    def _loop_worker(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _init_agent(self) -> ComputerControlAgent:
        gov_cfg = GovernorConfig.for_level(self.safety_level)
        return ComputerControlAgent(
            governor_config=gov_cfg,
            dry_run=self.dry_run,
            memory_path=self.memory_path,
            owner=self.owner,
        )

    def add_log_listener(self, listener: Callable[[str, str], None]) -> None:
        """Add a callback (text, tag) to stream agent updates into PySide6 UI."""
        if listener not in self._log_listeners:
            self._log_listeners.append(listener)

    def remove_log_listener(self, listener: Callable[[str, str], None]) -> None:
        if listener in self._log_listeners:
            self._log_listeners.remove(listener)

    def _emit_log(self, message: str, tag: str = "cyan") -> None:
        for listener in list(self._log_listeners):
            try:
                listener(message, tag)
            except Exception as e:
                _log.debug("Log listener notification warning: %s", e)

    # ── Memory Layer ──────────────────────────────────────────────────────────

    def get_memory_context(self, query: str, top_k: int = 5) -> str:
        """Build relevant memory context string to inject into LLM system prompt."""
        try:
            ctx = self._agent.memory_context(query)
            if ctx:
                lines = [l for l in ctx.splitlines() if l.strip().startswith("-")]
                self._emit_log(f"[Memory] প্রাসঙ্গিক তথ্য রিকল করা হয়েছে ({len(lines)} টি)", "gold")
            return ctx
        except Exception as e:
            _log.warning("Memory context build error: %s", e)
            return ""

    def remember(
        self,
        text: str,
        kind: str = "episode",
        key: Optional[str] = None,
        value: Optional[str] = None,
    ) -> bool:
        """Store a fact, preference, or episode into long-term SQLite memory."""
        try:
            m_kind = MemoryKind(kind)
        except ValueError:
            m_kind = MemoryKind.EPISODE

        try:
            rec = self._agent.remember(text, kind=m_kind, key=key, value=value)
            display = (rec.text[:50] + "...") if len(rec.text) > 50 else rec.text
            self._emit_log(f"[Memory] সংরক্ষিত: '{display}'", "ok")
            return True
        except Exception as e:
            _log.warning("Memory store error: %s", e)
            return False

    def preferred_int(self, key: str, default: int) -> int:
        """Get user's stored preference integer (e.g. volume.level, brightness.level)."""
        try:
            return self._agent.semantic.preferred_int(key, default)
        except Exception:
            return default

    def preferred_str(self, key: str, default: str) -> str:
        """Get user's stored preference string (e.g. default search query, favorite app)."""
        try:
            return self._agent.semantic.preferred_str(key, default)
        except Exception:
            return default

    def set_preference(self, key: str, value: str, text: Optional[str] = None) -> bool:
        """Explicitly set a user preference."""
        desc = text or f"User preferred {key} is {value}"
        return self.remember(desc, kind="preference", key=key, value=value)

    # ── Skills & Perception Layer ─────────────────────────────────────────────

    def quick(self, skill: str, **params: Any) -> Dict[str, Any]:
        """Synchronously execute a skill (volume, brightness, launch, youtube, screenshot)."""
        self._emit_log(f"[Skill] এক্সিকিউট হচ্ছে: {skill} {params or ''}", "cyan")
        try:
            coro = self._agent.quick(skill, **params)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            result = future.result(timeout=15.0)
            msg = result.detail or (f"Error: {result.error}" if result.error else "Done")
            res_dict = {
                "ok": result.ok,
                "detail": result.detail,
                "message": msg,
                "data": result.data,
                "error": result.error,
            }
            tag = "ok" if result.ok else "alert"
            self._emit_log(f"[Skill Result] {msg}", tag)
            return res_dict
        except Exception as e:
            self._emit_log(f"[Skill Error] {skill} ব্যর্থ: {e}", "alert")
            return {"ok": False, "message": str(e), "detail": str(e), "data": {}}

    def see(self, query: str = "*", save_path: str = "see.png") -> Dict[str, Any]:
        """Capture screen and perform OCR to read visual text."""
        self._emit_log(f"[Vision] স্ক্রিন রিড করা হচ্ছে (query: '{query}')...", "gold")
        try:
            coro = self._agent.see(query=query, save_path=save_path)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            result = future.result(timeout=20.0)
            msg = result.detail or (f"Error: {result.error}" if result.error else "Screen analyzed")
            tag = "ok" if result.ok else "alert"
            self._emit_log(f"[Vision] স্ক্রিন রিড সম্পন্ন: {msg}", tag)
            return {
                "ok": result.ok,
                "detail": result.detail,
                "message": msg,
                "data": result.data,
                "error": result.error,
            }
        except Exception as e:
            self._emit_log(f"[Vision Error] স্ক্রিন রিড ব্যর্থ: {e}", "alert")
            return {"ok": False, "message": str(e), "detail": str(e), "data": {}}

    # ── Autonomous Planning Layer ─────────────────────────────────────────────

    def run(self, goal: str, timeout: float = 60.0) -> Dict[str, Any]:
        """Run an autonomous multi-step goal using ReAct planner."""
        self._emit_log(f"[Agent Task] গোল শুরু: '{goal}'", "gold")
        try:
            coro = self._agent.run(goal)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            episode = future.result(timeout=timeout)
            tag = "ok" if episode.final_state.value == "succeeded" else "alert"
            self._emit_log(
                f"[Agent Task] সমাপ্ত ({episode.final_state.value}) - স্টেপ: {episode.step_count}",
                tag
            )
            return {
                "ok": episode.final_state.value == "succeeded",
                "state": episode.final_state.value,
                "step_count": episode.step_count,
                "goal": episode.goal,
                "steps": [
                    {
                        "skill": str(s.skill) if s.skill else None,
                        "thought": s.thought,
                        "success": s.success,
                        "observation": s.observation,
                    }
                    for s in episode.steps
                ]
            }
        except Exception as e:
            self._emit_log(f"[Agent Error] গোল ব্যর্থ: {e}", "alert")
            return {"ok": False, "error": str(e), "steps": []}

    def health(self) -> Dict[str, Any]:
        """Return full health snapshot."""
        try:
            return self._agent.health().as_dict()
        except Exception as e:
            return {"status": "error", "error": str(e)}


def get_agent_bridge(dry_run: bool = False) -> AgentBridge:
    """Access the global AgentBridge singleton instance."""
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                _INSTANCE = AgentBridge(dry_run=dry_run)
    return _INSTANCE
